"""The public read filter and ranking rules the map, globe and story pages share.

Every rule here answers one question a reader's query asks — which stories count
as published, which tiers count as corroboration, how wide the window is, which
headlines are the same story — and the three public routers answer it identically
because they all call into this module. Nothing here touches FastAPI routing or
renders anything, so the rules are testable on their own.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy import select, desc, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import Event, RawArticle, ReportingUnit, Story, StoryUnitLink

# Public map defaults.
#
# Corroboration is not a Story.status value: Story.Status has no
# "corroborated" member. The pipeline expresses it through the tier 1 counts,
# and apply_tier1_gate in src/verification/tiers.py queues a story only when
# at least 2 tier 1 units arrive from at least 2 distinct owner groups. The
# map reuses exactly that rule rather than inventing a new status: stories
# need tier1_unit_count >= 2 and distinct_owners >= 2, and an event counts as
# corroborated when its tier1_source_count reaches the same threshold.
MAP_CORROBORATION_MIN_TIER1 = 2
MAP_CORROBORATION_MIN_OWNERS = 2

# The default window for a map that loads with no explicit filters. Kept in
# the 24 to 48 hour band; wider windows are an explicit user choice.
MAP_DEFAULT_WINDOW_HOURS = 48
MAP_MAX_WINDOW_HOURS = 24 * 365

# Public event payloads (/api/globe/events and /api/map/replay) are anonymous, so
# a requested limit is clamped rather than trusted: hours=0 is unbounded and an
# unclamped limit would let one caller ask for the whole events table.
MAP_EVENTS_MAX_LIMIT = 1000

# Public map surfaces: top stories list, per story page and the freshness stamp.
#
# Everything below is read only and anonymous safe. The serializer deliberately
# omits the curation queue's internal fields (Story.status, Story.gate_reason,
# CuratedPost rows, claim evidence), so nothing here reports what a curator has
# decided or is about to decide.
#
# The story queries are filtered by the same rule the authenticated routes'
# comments give for staying behind auth: only what a curator has approved is
# published. PENDING is the triage queue, BLOCKED failed the gate, REJECTED and
# EXPIRED were closed out, so an anonymous reader asking for one of those story
# ids gets the same 404 as for an id that never existed.
PUBLIC_STORY_STATUSES = (Story.Status.QUEUED, Story.Status.POSTED)


# ---------------------------------------------------------------------------
# Discovery: tier filter, ranked topic search, near-duplicate collapsing.
# ---------------------------------------------------------------------------

# Cache for the pg_trgm probe: None = not probed yet.
_trigram_available_cache: bool | None = None


async def _trigram_available(session: AsyncSession) -> bool:
    """True when the pg_trgm extension is installed (Postgres dev/prod).

    SQLite test databases never have it; search falls back to ILIKE there.
    The result is cached per process because extensions do not change at runtime.
    """
    global _trigram_available_cache
    if _trigram_available_cache is not None:
        return _trigram_available_cache
    try:
        from sqlalchemy import text as _text

        result = await session.execute(
            _text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        )
        _trigram_available_cache = result.first() is not None
    except Exception:
        # SQLite (tests) or any failure: no trigram support.
        _trigram_available_cache = False
    return _trigram_available_cache


def normalize_headline(headline: str | None) -> str:
    """Canonical form for near-duplicate headline comparison.

    Lowercase, strip punctuation, collapse whitespace. Applied to the ENGLISH
    headline so the same story reported in French and English still clusters.
    """
    text = (headline or "").lower()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tier_columns() -> list:
    return [
        Story.tier1_unit_count,
        Story.tier2_unit_count,
        Story.tier3_unit_count,
        Story.tier4_unit_count,
    ]


def _tier_include_condition(tiers: list[int] | None):
    """Story passes when it has at least one unit in an included tier.

    tiers is a list like [1, 2]; None/empty means all tiers. This is what lets
    a reader hide tier 3: stories carried only by tier-3 outlets drop out,
    while tier-1 stories that also have tier-3 units stay.
    """
    if not tiers:
        return None
    wanted = {t for t in tiers if t in (1, 2, 3, 4)}
    if not wanted or len(wanted) == 4:
        return None
    cols = _tier_columns()
    return or_(*[cols[t - 1] > 0 for t in sorted(wanted)])


def _parse_tiers(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        tiers = [int(p) for p in raw.split(",") if p.strip()]
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tiers must be a comma-separated list like '1,2'.",
        )
    tiers = [t for t in tiers if t in (1, 2, 3, 4)]
    return tiers or None


def _apply_story_tier_filter(stmt, tiers: list[int] | None):
    """Restrict an Event query to events whose story has a unit in an included tier.

    Joins Event -> Story once; the tier mix lives on the story. None/empty
    tiers leave the statement untouched.
    """
    condition = _tier_include_condition(tiers)
    if condition is None:
        return stmt
    return stmt.join(Story, Story.id == Event.story_id).where(condition)


def _verification_badge(story: Story) -> dict:
    """Unmissable corroboration state for one story.

    Outlets count plus the tier mix, so a reader sees at a glance whether a
    story stands on tier-1 reporting or a pile of tier-3 aggregators.
    """
    mix = {
        "t1": int(story.tier1_unit_count or 0),
        "t2": int(story.tier2_unit_count or 0),
        "t3": int(story.tier3_unit_count or 0),
        "t4": int(story.tier4_unit_count or 0),
    }
    best_tier = next((i + 1 for i, k in enumerate(["t1", "t2", "t3", "t4"]) if mix[k] > 0), None)
    corroborated = (
        mix["t1"] >= MAP_CORROBORATION_MIN_TIER1
        and int(story.distinct_owners or 0) >= MAP_CORROBORATION_MIN_OWNERS
    )
    outlets = int(story.distinct_owners or 0)
    if corroborated:
        label = f"Corroborated · {outlets} outlet{'s' if outlets != 1 else ''}"
    elif outlets >= 2:
        label = f"{outlets} outlets · needs tier-1 confirmation"
    elif outlets == 1:
        label = "Single outlet · unconfirmed"
    else:
        label = "No outlets yet"
    return {
        "outlets": outlets,
        "tier_mix": mix,
        "best_tier": best_tier,
        "corroborated": corroborated,
        "label": label,
    }


async def _search_story_ids(
    session: AsyncSession,
    query: str,
    limit: int = 50,
) -> list:
    """Ranked story search over article headlines (English preferred).

    pg_trgm similarity ranking on Postgres; ILIKE fallback elsewhere (tests).
    Searches both the translated and original headlines so a French query can
    still find the story via its English translation and vice versa. Returns
    story ids ordered best match first.
    """
    query = (query or "").strip()
    if not query:
        return []
    trigram = await _trigram_available(session)

    rep_join = (
        select(ReportingUnit.id, StoryUnitLink.story_id)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .subquery()
    )

    if trigram:
        from sqlalchemy import text as _text

        # Similarity against the English headline first, then the original;
        # take the better of the two so neither language is penalized.
        sim = func.greatest(
            func.similarity(func.coalesce(RawArticle.title_en, ""), query),
            func.similarity(func.coalesce(RawArticle.title, ""), query),
        )
        stmt = (
            select(rep_join.c.story_id, func.max(sim).label("score"))
            .join(
                ReportingUnit,
                ReportingUnit.id == rep_join.c.id,
            )
            .join(
                RawArticle,
                RawArticle.id == ReportingUnit.representative_article_id,
            )
            .where(
                or_(
                    _text("COALESCE(raw_articles.title_en, '') % :q").bindparams(q=query),
                    _text("COALESCE(raw_articles.title, '') % :q").bindparams(q=query),
                )
            )
            .group_by(rep_join.c.story_id)
            .order_by(desc("score"))
            .limit(limit)
        )
    else:
        like = f"%{query}%"
        stmt = (
            select(rep_join.c.story_id, func.max(RawArticle.fetched_at).label("score"))
            .join(ReportingUnit, ReportingUnit.id == rep_join.c.id)
            .join(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
            .where(
                or_(
                    RawArticle.title_en.ilike(like),
                    RawArticle.title.ilike(like),
                )
            )
            .group_by(rep_join.c.story_id)
            .order_by(desc("score"))
            .limit(limit)
        )
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


def _resolve_window(
    hours: int,
    now: datetime,
) -> Tuple[datetime, datetime]:
    """Turn an hours-bounded window into an inclusive (start, end) pair.

    hours of None or 0 means unbounded, which is how the UI asks for full
    history explicitly. The upper bound is "now", so a request can never reach
    into the future.
    """
    if not hours or hours <= 0:
        return None, now
    hours = min(int(hours), MAP_MAX_WINDOW_HOURS)
    return now - timedelta(hours=hours), now


def _corroboration_filter(min_tier1_sources: int) -> int:
    """Normalize the tier 1 corroboration threshold.

    None keeps the endpoint default (the 2 tier 1 threshold the pipeline gate
    uses). 0 or less means "no corroboration filter", which is what the UI
    sends when someone turns the filter off.
    """
    if min_tier1_sources is None:
        return MAP_CORROBORATION_MIN_TIER1
    try:
        value = int(min_tier1_sources)
    except (TypeError, ValueError):
        return MAP_CORROBORATION_MIN_TIER1
    return max(0, value)


def _parse_iso_timestamp(value: str, param: str) -> datetime | None:
    """Parse an optional ISO 8601 query parameter into an aware datetime.

    Blank/absent returns None. A date-only value (2026-09-30) means midnight
    UTC. Naive timestamps are assumed UTC, which matches how Event.start_time
    is stored.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid {param} timestamp: {value!r}. "
                "Expected ISO 8601, e.g. 2026-09-30T00:00:00Z or 2026-09-30."
            ),
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed