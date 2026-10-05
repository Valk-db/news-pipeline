"""The anonymous reader surfaces: one story, one proof, and the map page.

Every route here serves read-only HTML to callers with no credentials, so each
one is written to disclose corroboration and nothing about the curation queue:
no status, no gate reason, no curator decision. Story pages filter on
PUBLIC_STORY_STATUSES so an id still sitting in the triage queue is a 404 to an
anonymous reader, the same answer as an id that never existed.
"""

import uuid
from datetime import datetime, UTC

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, select

from curation_ui.app_state import check_database_public, render_error_page, templates
from curation_ui.discovery import (
    MAP_CORROBORATION_MIN_OWNERS,
    MAP_CORROBORATION_MIN_TIER1,
    MAP_DEFAULT_WINDOW_HOURS,
    PUBLIC_STORY_STATUSES,
    _parse_tiers,
    _verification_badge,
)
from curation_ui.map_api import _collect_map_freshness, _collect_top_stories
from curation_ui.proofs import build_proof_view
from src.schema.models import Event, RawArticle, ReportingUnit, Story, StoryUnitLink
from src.shared.database import get_session

router = APIRouter()

# Per-story page size for an anonymous reader.
#
# The story page is keyed on a story id and needs no request parameter to reach
# a big story: one event covered by two hundred outlets has two hundred reporting
# units, and the page fetched every one of them plus every event. Both lists are
# capped here, one row over the cap is fetched to know whether the list was
# complete, and the page says so out loud rather than presenting a truncated
# evidence list as the whole one.
PUBLIC_STORY_UNIT_CAP = 200
PUBLIC_STORY_EVENT_CAP = 200


@router.get("/stories/{story_id}", response_class=HTMLResponse)
async def public_story_page(story_id: uuid.UUID, request: Request):
    """Public read only view of one story's corroboration.

    Shows the source articles and the corroboration counts, and nothing about
    the curation queue: no status, no gate reason, no curator decisions. Only
    stories a curator has approved are served; anything else in the queue is a
    404 to an anonymous caller (see PUBLIC_STORY_STATUSES).
    """
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = select(Story).where(
            Story.id == story_id,
            Story.status.in_(PUBLIC_STORY_STATUSES),
        )
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        units_stmt = (
            select(ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story_id)
            .limit(PUBLIC_STORY_UNIT_CAP + 1)
        )
        units_result = await session.execute(units_stmt)
        units = units_result.scalars().all()
        units_truncated = len(units) > PUBLIC_STORY_UNIT_CAP
        units = units[:PUBLIC_STORY_UNIT_CAP]

        # The true unit total, counted rather than inferred from the capped page,
        # so the header can state the real size of the story.
        unit_count = (
            await session.execute(
                select(func.count())
                .select_from(StoryUnitLink)
                .where(StoryUnitLink.story_id == story_id)
            )
        ).scalar() or 0

        articles = []
        for unit in units:
            representative = getattr(unit, "representative", None)
            if representative is None:
                continue
            title_en = getattr(representative, "title_en", None) or None
            lang = getattr(representative, "detected_language", None) or None
            tier_value = (
                representative.source_tier.value if representative.source_tier else None
            )
            tier_num = {"tier1": 1, "tier2": 2, "tier3": 3, "tier4": 4}.get(tier_value)
            articles.append({
                "id": str(representative.id),
                "log_index": representative.log_index,
                "title": title_en or representative.title,
                "title_original": representative.title,
                "lang": lang,
                "translated": bool(title_en and title_en != representative.title),
                "url": representative.url,
                "source_domain": representative.source_domain,
                "source_tier": tier_value,
                "tier_num": tier_num,
                "published_at": representative.published_at,
            })
        articles.sort(
            key=lambda item: (item["published_at"] is None, item["published_at"]),
            reverse=False,
        )

        events_stmt = (
            select(Event)
            .where(Event.story_id == story_id)
            .order_by(desc(Event.start_time))
            .limit(PUBLIC_STORY_EVENT_CAP + 1)
        )
        events_result = await session.execute(events_stmt)
        events = list(events_result.scalars().all())
        events_truncated = len(events) > PUBLIC_STORY_EVENT_CAP
        events = events[:PUBLIC_STORY_EVENT_CAP]

        latest_at = max(
            (event.start_time for event in events if event.start_time is not None),
            default=None,
        )

    headline = next((item["title"] for item in articles if item["title"]), "Untitled story")

    return templates.TemplateResponse(request, "story.html", {
        "request": request,
        "story_id": str(story.id),
        "headline": headline,
        "articles": articles,
        "outlets": int(story.distinct_owners or 0),
        "tier1_units": int(story.tier1_unit_count or 0),
        "units": int(unit_count),
        "units_truncated": units_truncated,
        "day": story.day,
        "latest_at": latest_at,
        "corroborated": (
            int(story.tier1_unit_count or 0) >= MAP_CORROBORATION_MIN_TIER1
            and int(story.distinct_owners or 0) >= MAP_CORROBORATION_MIN_OWNERS
        ),
        "verification": _verification_badge(story),
        "events_truncated": events_truncated,
        "events": [
            {
                "location_name": event.location_name,
                "event_type": event.event_type.value if event.event_type else None,
                "start_time": event.start_time,
                "confidence": float(event.confidence) if event.confidence is not None else None,
                "source_count": event.source_count,
                "tier1_source_count": event.tier1_source_count,
            }
            for event in events
        ],
    })


# Inclusion proof permalinks
#
# One public, read only page per archived article: /proof/{article_id}. The
# proof steps are server-rendered so the page is complete before JS runs, and
# an article that has not been stamped yet renders an honest "proof pending"
# page rather than a fabricated proof.


@router.get("/proof/{article_id}", response_class=HTMLResponse)
async def public_proof_page(article_id: uuid.UUID, request: Request):
    """Public read only inclusion proof for one archived article.

    Shows the article's identity, its log entry, the sibling path that folds
    the entry's leaf hash up to a signed checkpoint root, and the checkpoint
    itself, with every intermediate digest so a reader can recompute the root
    by hand.
    """
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        article = await session.get(RawArticle, article_id)
        if article is None:
            raise HTTPException(404, "Article not found")
        view = await build_proof_view(session, article)

    return templates.TemplateResponse(request, "proof.html", {
        "request": request,
        "view": view,
    })


# Flat map page route
@router.get("/map", response_class=HTMLResponse)
async def map_page(
    request: Request,
    tiers: str = None,
    sort: str = "top",
    q: str = None,
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
):
    """Flat 2D map visualization page. Public and read only.

    The list of top stories and the freshness stamp are rendered server side so
    the page is useful before any JavaScript runs; map.js refreshes both from
    the public JSON endpoints. Discovery controls (tier filter, sort, search,
    date window) are server-rendered in their initial state and stay in sync
    with the query string, so first paint already reflects the reader's filters.
    """
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    if sort not in ("top", "newest", "oldest"):
        sort = "top"

    now = datetime.now(UTC)
    async with get_session() as session:
        freshness = await _collect_map_freshness(session, now)
        top_stories = await _collect_top_stories(
            session,
            now=now,
            hours=hours,
            tiers=_parse_tiers(tiers),
            sort=sort,
            q=q,
        )

    return templates.TemplateResponse(request, "map.html", {
        "request": request,
        "freshness": freshness,
        "top_stories": top_stories,
        "default_window_hours": MAP_DEFAULT_WINDOW_HOURS,
        "corroboration_min_tier1": MAP_CORROBORATION_MIN_TIER1,
        "active_tiers": _parse_tiers(tiers) or [1, 2, 3, 4],
        "active_sort": sort,
        "active_q": q or "",
        "active_hours": hours,
    })