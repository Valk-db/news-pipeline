"""The public flat-map JSON API: the freshness stamp, the top stories list, and
the replay window.

The replay route runs the same event query as /api/globe/events (see
curation_ui.events) and its window defaults live here next to the other map
constants. The freshness stamp and the story serializer are the two pieces the
public map page also needs, which is why the page imports them from here rather
than re-deriving them.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from curation_ui.app_state import check_database_public
from curation_ui.discovery import (
    MAP_CORROBORATION_MIN_OWNERS,
    MAP_CORROBORATION_MIN_TIER1,
    MAP_DEFAULT_WINDOW_HOURS,
    MAP_EVENTS_MAX_LIMIT,
    MAP_MAX_WINDOW_HOURS,
    PUBLIC_STORY_STATUSES,
    _apply_story_tier_filter,
    _corroboration_filter,
    _parse_iso_timestamp,
    _parse_tiers,
    _resolve_window,
    _search_story_ids,
    _tier_include_condition,
    _verification_badge,
    normalize_headline,
)
from curation_ui.events import _event_conditions, _event_feature
from src.schema.models import Event, RawArticle, ReportingUnit, Story, StoryUnitLink
from src.shared.database import get_session
from src.verification.event_identity import (
    CANONICAL_EVENT_ID,
    cluster_corroboration,
)

router = APIRouter()

# Freshness stamp window and query guards.
MAP_FRESHNESS_WINDOW_HOURS = 24
MAP_FRESHNESS_OUTLET_STORY_CAP = 5000

# Top stories list.
MAP_TOP_STORIES_DEFAULT_LIMIT = 12
MAP_TOP_STORIES_MAX_LIMIT = 50

# Map replay API
#
# Replay sends the entire time window in a single response so the browser can
# scrub back and forth without refetching. That makes the result count the cost
# driver (payload size plus per-marker work in the client), so every response is
# capped at MAP_EVENTS_MAX_LIMIT events and the requested limit is clamped into
# [1, MAP_EVENTS_MAX_LIMIT]. Over-cap windows come back with "truncated": true
# and the oldest events dropped — narrow the window or filter by event_type
# rather than raising the cap.
MAP_REPLAY_DEFAULT_LIMIT = 500


def _as_utc(value: datetime) -> datetime:
    """Read a timestamp back as aware UTC.

    SQLite has no timezone type, so a DateTime(timezone=True) column comes back
    naive there. Event.start_time and Story.updated_at are both stored as UTC,
    so tagging it is the correct read rather than a guess.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _relative_age(latest_at: datetime, now: datetime) -> str:
    """Coarse age string for the freshness stamp. Never negative, never NaN."""
    if latest_at is None:
        return ""
    latest_at = _as_utc(latest_at)
    now = _as_utc(now)
    seconds = (now - latest_at).total_seconds()
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    if minutes < 2:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'} ago"


def format_freshness_stamp(
    latest_at: datetime,
    outlet_count: int,
    corroborated_events: int,
    now: datetime,
    window_hours: int = MAP_FRESHNESS_WINDOW_HOURS,
) -> str:
    """Render the map's freshness line from already counted numbers.

    Pure so the empty and stale cases are testable without a database. With no
    timestamp at all the line says so plainly instead of showing zeroes as if
    they were measurements.
    """
    outlets = max(0, int(outlet_count or 0))
    corroborated = max(0, int(corroborated_events or 0))
    window = f"({window_hours}h)"

    if latest_at is None:
        return "No events recorded yet"

    age = _relative_age(latest_at, now)
    corroborated_part = f"{corroborated} corroborated event{'' if corroborated == 1 else 's'} {window}"
    # "No corroborated events" is the honest line when outlets exist in the
    # window but none of them clear the gate. With zero outlets there is
    # nothing behind the window at all, so the plain updated line reads better
    # than a denial about corroboration.
    if corroborated or outlets == 0:
        return f"Updated {age}, {outlets} outlet{'' if outlets == 1 else 's'}, {corroborated_part}"
    return f"No corroborated events {window}, {outlets} outlet{'' if outlets == 1 else 's'}, last event {age}"


async def _collect_map_freshness(session: AsyncSession, now: datetime) -> dict:
    """Gather the raw numbers behind the freshness stamp.

    Outlet counts come from the same chain the map pages read: distinct
    (story, source_domain) pairs across the reporting units of every story,
    so one outlet covering one story counts once no matter how many units it
    fed. The pair count is deliberately not windowed: a quiet day should still
    show the outlets behind the older stories. The story set is capped so the
    stamp cannot turn into a full table scan of the article table.
    """
    window_start = now - timedelta(hours=MAP_FRESHNESS_WINDOW_HOURS)

    latest_event = (
        await session.execute(select(func.max(Event.start_time)))
    ).scalar()
    latest_story = (
        await session.execute(select(func.max(Story.updated_at)))
    ).scalar()
    # Event times drive the stamp: a story row touched by curation today whose
    # events are six days old is stale map data, not a fresh update. The story
    # timestamp is only a fallback for a database that has stories but no
    # geocoded events yet.
    latest_at = _as_utc(latest_event)
    if latest_at is None:
        latest_at = _as_utc(latest_story)

    corroborated_events = (
        await session.execute(
            select(func.count(Event.id)).where(
                Event.start_time >= window_start,
                Event.tier1_source_count >= MAP_CORROBORATION_MIN_TIER1,
            )
        )
    ).scalar() or 0

    outlet_pairs = (
        select(StoryUnitLink.story_id, RawArticle.source_domain)
        .select_from(RawArticle)
        .join(ReportingUnit, ReportingUnit.representative_article_id == RawArticle.id)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(
            StoryUnitLink.story_id.in_(
                select(Story.id).limit(MAP_FRESHNESS_OUTLET_STORY_CAP)
            )
        )
        .distinct()
        .subquery()
    )
    outlet_count = (
        await session.execute(select(func.count()).select_from(outlet_pairs))
    ).scalar() or 0

    return {
        "stamp": format_freshness_stamp(latest_at, outlet_count, corroborated_events, now),
        "latest_at": latest_at.isoformat() if latest_at is not None else None,
        "updated_minutes_ago": (
            int(max(0.0, (_as_utc(now) - latest_at).total_seconds()) // 60)
            if latest_at is not None else None
        ),
        "outlets": max(0, int(outlet_count or 0)),
        "corroborated_events": max(0, int(corroborated_events or 0)),
        "window_hours": MAP_FRESHNESS_WINDOW_HOURS,
    }


@router.get("/api/map/freshness")
async def get_map_freshness(request: Request):
    """Freshness stamp for the public map. Read only, degrades to an empty stamp."""
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return {"error": db_msg}

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        return await _collect_map_freshness(session, now)


def _public_story_summary(
    story: Story,
    headline: str,
    event_count: int,
    latest: datetime,
    headline_en: str | None = None,
    lang: str | None = None,
) -> dict:
    """Public projection of one Story: what the pipeline corroborated, nothing else.

    headline_en is the translated headline when the original is not English;
    the display headline prefers it (COALESCE semantics: English when
    available, original otherwise, never stored twice). lang tags the original
    language so a translated headline is honest about its source.
    """
    return {
        "story_id": str(story.id),
        "headline": headline_en or headline or "Untitled story",
        "headline_original": headline or "Untitled story",
        "lang": lang,
        "translated": bool(headline_en and headline_en != headline),
        "outlets": int(story.distinct_owners or 0),
        "tier1_units": int(story.tier1_unit_count or 0),
        "units": (
            int(story.tier1_unit_count or 0)
            + int(story.tier2_unit_count or 0)
            + int(story.tier3_unit_count or 0)
            + int(story.tier4_unit_count or 0)
        ),
        "event_count": int(event_count or 0),
        "day": story.day.isoformat() if story.day else None,
        "latest_at": latest.isoformat() if latest else None,
        "corroborated": (
            int(story.tier1_unit_count or 0) >= MAP_CORROBORATION_MIN_TIER1
            and int(story.distinct_owners or 0) >= MAP_CORROBORATION_MIN_OWNERS
        ),
        "verification": _verification_badge(story),
        "href": f"/stories/{story.id}",
    }


async def _collect_top_stories(
    session: AsyncSession,
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
    min_owners: int = MAP_CORROBORATION_MIN_OWNERS,
    limit: int = MAP_TOP_STORIES_DEFAULT_LIMIT,
    now: datetime = None,
    tiers: list[int] | None = None,
    sort: str = "top",
    q: str | None = None,
    start: datetime = None,
    end: datetime = None,
) -> list:
    """Stories ranked by independent outlet count, using the existing gate fields.

    Discovery controls:
      tiers: only stories with at least one unit in these tiers (e.g. [1, 2]
        hides tier-3-only stories). None means all tiers.
      sort: "top" (corroboration rank), "newest", or "oldest" by latest event.
      q: ranked topic search over headlines; overrides sort with best-match order.
      start/end: explicit ISO window on event start_time; wins over hours.

    Near-duplicate headlines (same story from several outlets, or the same
    wire copy in two languages) collapse to one entry: the highest-ranked
    story per normalized English headline wins. The UI shows one row per
    story, never five rows for five outlets.

    Stories a curator has not approved are not listed at all (see
    PUBLIC_STORY_STATUSES): the queue is not the public map.
    """
    now = now or datetime.now(timezone.utc)
    if start is not None or end is not None:
        window_start, window_end = start, now if end is None else end
    else:
        window_start, window_end = _resolve_window(hours, now)
    limit = max(1, min(int(limit or MAP_TOP_STORIES_DEFAULT_LIMIT), MAP_TOP_STORIES_MAX_LIMIT))

    # Ranked search short-circuits the normal listing: fetch matching story
    # ids in best-match order, then hydrate in that order.
    search_order: list | None = None
    if q and q.strip():
        search_order = await _search_story_ids(session, q.strip(), limit=limit * 2)
        if not search_order:
            return []

    # One count per pin: a story's rows are grouped by the canonical event they report, so
    # two rows for one occurrence count once, exactly as the globe draws it once. The
    # window still filters on the story's own rows, so collapsing events never removes a
    # story from the list -- the story reported something that happened in the window, and
    # a shared pin is not a reason to stop saying so.
    event_stmt = (
        select(
            Event.story_id,
            func.count(func.distinct(CANONICAL_EVENT_ID)).label("event_count"),
            func.max(Event.start_time).label("latest_at"),
        )
        .group_by(Event.story_id)
    )
    if window_start is not None:
        event_stmt = event_stmt.where(Event.start_time >= window_start)
    if window_end is not None:
        event_stmt = event_stmt.where(Event.start_time <= window_end)
    event_stmt = event_stmt.subquery()

    stmt = (
        select(Story, event_stmt.c.event_count, event_stmt.c.latest_at)
        .join(event_stmt, event_stmt.c.story_id == Story.id)
        .where(Story.status.in_(PUBLIC_STORY_STATUSES))
        .options(selectinload(Story.units).selectinload(ReportingUnit.representative))
    )

    tier_condition = _tier_include_condition(tiers)
    if tier_condition is not None:
        stmt = stmt.where(tier_condition)

    if search_order is not None:
        stmt = stmt.where(Story.id.in_(search_order))
    elif min_owners and int(min_owners) > 0:
        stmt = stmt.where(
            Story.distinct_owners >= int(min_owners),
            Story.tier1_unit_count >= MAP_CORROBORATION_MIN_TIER1,
        )

    if search_order is not None:
        # Hydrate then order by the search ranking in Python (portable).
        pass
    elif sort == "newest":
        stmt = stmt.order_by(desc(event_stmt.c.latest_at))
    elif sort == "oldest":
        stmt = stmt.order_by(event_stmt.c.latest_at.asc())
    else:  # "top": corroboration rank, the default
        stmt = stmt.order_by(
            desc(Story.distinct_owners),
            desc(Story.tier1_unit_count),
            desc(event_stmt.c.latest_at),
        )
    stmt = stmt.limit(limit * 2)  # over-fetch: near-dupe collapsing trims below

    result = await session.execute(stmt)
    rows = result.all()

    summaries = []
    for story, event_count, latest_at in rows:
        headline = ""
        headline_en = None
        lang = None
        for unit in story.units:
            representative = getattr(unit, "representative", None)
            if representative is not None and representative.title:
                headline = representative.title
                headline_en = getattr(representative, "title_en", None) or None
                lang = getattr(representative, "detected_language", None) or None
                break
        summaries.append(
            (
                story,
                _public_story_summary(
                    story, headline, event_count or 0, latest_at,
                    headline_en=headline_en, lang=lang,
                ),
            )
        )

    if search_order is not None:
        rank = {str(sid): i for i, sid in enumerate(search_order)}
        summaries.sort(key=lambda pair: rank.get(str(pair[0].id), 10**9))

    # Collapse near-duplicates: same normalized English headline, one entry.
    seen: set[str] = set()
    deduped = []
    for story, summary in summaries:
        key = normalize_headline(summary["headline"])
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(summary)
        if len(deduped) >= limit:
            break
    return deduped


def _effective_window_hours(
    hours: int,
    start: datetime | None,
    end: datetime | None,
) -> int | None:
    """The hours the query really used, or None when the window is unbounded.

    Mirrors _collect_top_stories' own choice: an explicit start/end pair wins
    over hours, and hours of 0 (or absent) means unbounded.
    """
    if start is not None or end is not None:
        if start is None or end is None:
            return None
        return int(max(0.0, (end - start).total_seconds()) // 3600)
    if not hours or hours <= 0:
        return None
    return min(int(hours), MAP_MAX_WINDOW_HOURS)


@router.get("/api/map/stories")
async def get_map_stories(
    request: Request,
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
    min_owners: int = MAP_CORROBORATION_MIN_OWNERS,
    limit: int = MAP_TOP_STORIES_DEFAULT_LIMIT,
    tiers: str = None,
    sort: str = "top",
    q: str = None,
    start: str = None,
    end: str = None,
):
    """Top stories for the public map list, ranked by corroboration. Read only.

    Lists approved stories only (see PUBLIC_STORY_STATUSES). Discovery params:
    tiers (comma list like "1,2", stories need a unit in an included tier),
    sort (top|newest|oldest), q (ranked topic search over headlines), start/end
    (ISO 8601 window on event time, wins over hours).
    """
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return {"error": db_msg}

    if sort not in ("top", "newest", "oldest"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sort must be one of: top, newest, oldest.",
        )
    start_dt = _parse_iso_timestamp(start, "start") if start else None
    end_dt = _parse_iso_timestamp(end, "end") if end else None
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start must be earlier than or equal to end.",
        )

    async with get_session() as session:
        stories = await _collect_top_stories(
            session,
            hours=hours,
            min_owners=min_owners,
            limit=limit,
            tiers=_parse_tiers(tiers),
            sort=sort,
            q=q,
            start=start_dt,
            end=end_dt,
        )

    return {
        "stories": stories,
        "count": len(stories),
        "window_hours": hours,
        # hours is echoed above as asked for; this is what the query actually
        # used. _resolve_window clamps to MAP_MAX_WINDOW_HOURS, so an
        # anonymous caller asking for hours=999999 was served a 1 year window.
        "effective_window_hours": _effective_window_hours(hours, start_dt, end_dt),
        "min_owners": min_owners,
        "tiers": tiers,
        "sort": sort,
        "q": q,
    }


@router.get("/api/map/replay")
async def get_map_replay(
    request: Request,
    start: str = None,  # ISO 8601, inclusive
    end: str = None,  # ISO 8601, inclusive
    event_type: str = None,
    min_tier1_sources: int = None,
    limit: int = MAP_REPLAY_DEFAULT_LIMIT,
    tiers: str = None,
):
    """Get a chronological event window for the flat map's replay scrubber.

    Public and read only, same event query path as /api/globe/events, ordered
    oldest first so the client can reveal markers by start_time. start/end
    bound the window inclusively and are optional (absent = unbounded). The
    window is fetched worldwide rather than per-bbox: the scrubber has to hold
    the whole timeline to jump anywhere, and panning mid-replay must not drop
    markers.

    min_tier1_sources defaults to the corroboration threshold
    (MAP_CORROBORATION_MIN_TIER1); pass 0 for every event regardless of how
    many tier 1 outlets carry it.

    Returns a GeoJSON FeatureCollection (identical feature shape to
    /api/globe/events, so the map's marker rendering is reused verbatim) plus
    window metadata as foreign members. Result count is capped at
    MAP_EVENTS_MAX_LIMIT; see the comment above that constant.

    Like the globe endpoint it excludes every story that is not public, via
    PUBLIC_STORY_STATUSES inside _event_conditions.
    """
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return {"error": db_msg}

    start_dt = _parse_iso_timestamp(start, "start")
    end_dt = _parse_iso_timestamp(end, "end")
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start must be earlier than or equal to end.",
        )

    effective_limit = max(1, min(limit, MAP_EVENTS_MAX_LIMIT))
    threshold = _corroboration_filter(min_tier1_sources)

    async with get_session() as session:
        stmt = select(Event).options(selectinload(Event.geometry), selectinload(Event.layer))
        stmt = _apply_story_tier_filter(stmt, _parse_tiers(tiers))

        conditions = _event_conditions(
            event_type=event_type,
            start=start_dt,
            end=end_dt,
            min_tier1_sources=threshold or None,
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Oldest first, id as a tiebreak so equal start_times keep a stable order.
        # One extra row is fetched purely to report truncation accurately.
        stmt = stmt.order_by(Event.start_time.asc(), Event.id.asc()).limit(effective_limit + 1)
        result = await session.execute(stmt)
        events = list(result.scalars().all())
        corroboration = await cluster_corroboration(session, events)

    truncated = len(events) > effective_limit
    events = events[:effective_limit]

    return {
        "type": "FeatureCollection",
        "features": [_event_feature(e, corroboration.get(e.id)) for e in events],
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "count": len(events),
        "limit": effective_limit,
        "max_limit": MAP_EVENTS_MAX_LIMIT,
        "truncated": truncated,
    }


# MAP_MAX_WINDOW_HOURS bounds the hours parameter of both this router's window
# endpoints; re-exported here so callers importing the map surface get the whole
# set from one module.
__all__ = [
    "router",
    "format_freshness_stamp",
    "MAP_FRESHNESS_WINDOW_HOURS",
    "MAP_FRESHNESS_OUTLET_STORY_CAP",
    "MAP_TOP_STORIES_DEFAULT_LIMIT",
    "MAP_TOP_STORIES_MAX_LIMIT",
    "MAP_REPLAY_DEFAULT_LIMIT",
    "MAP_MAX_WINDOW_HOURS",
]