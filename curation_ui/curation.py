"""The curation queue: a read-only reading surface over what the pipeline produced.

Every route here is behind require_auth. There are deliberately no state-changing
routes: a story leaves PENDING through the gate and the cleanup job, never through
a button in this UI, so the queue is a place you read rather than a place you act.

Pages render server side. _render_stories_grid is the unit of work behind the
queue and returns only what a card can honestly show; _story_detail_bundle is the
unit of work behind the detail page and is the one place the claim and narrative
internals are still loaded.
"""

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from curation_ui.app_state import check_database, render_error_page, templates
from curation_ui.discovery import (
    _parse_tiers,
    _resolve_window,
    _search_story_ids,
    _tier_include_condition,
    _verification_badge,
)
from curation_ui.security import require_auth
from src.schema.models import (
    CanonicalEntity,
    Claim,
    ClaimEvidence,
    EdgePredicate,
    EntityEdge,
    MediaAsset,
    RawArticle,
    ReportingUnit,
    Snippet,
    SourceReliabilitySnapshot,
    Story,
    StoryTopicGroup,
    StoryUnitLink,
    TopicGroup,
)
from src.shared.database import get_session

router = APIRouter()

logger = logging.getLogger(__name__)

# Shown when a story has no representative article title to name it by. The
# Story row itself has no headline column (see src/schema/models.py), so an
# untitled story is a real possibility and the card must say so rather than
# render blank.
NO_HEADLINE = "No headline captured"

# Discovery controls for the triage queue.
#
# The queue defaults are deliberately the ones the queue already had: newest
# first, every tier, no window. A curator opens the workbench to see what just
# arrived, so narrowing is opt in. The filter rules themselves live in
# discovery.py, shared with the public map, so a tier means the same thing on
# both surfaces.
CURATION_QUEUE_LIMIT = 50
CURATION_SORT_DEFAULT = "newest"
CURATION_SORTS = ("top", "newest", "oldest")

# How many stories the topic search ranks before the other filters trim the
# result. Over-fetched because a search match outside the window or outside the
# selected tiers still counts as a match the curator typed.
CURATION_SEARCH_LIMIT = 200

# How many quoted snippets the detail page shows. They are supporting quotes for
# the claims, not the story, so the page shows the strongest few.
DETAIL_SNIPPET_CAP = 3

# How much of the first reporting outlet's own text the detail page quotes. A
# full article body would bury the sources list on a phone; this is enough to say
# what the story is about, with the rest one tap away at the source.
DETAIL_SUMMARY_CHARS = 600

# Human wording for each tier, lifted from the definitions in
# src/verification/tiers.py so the queue, the detail page and the public map all
# describe a tier the same way. Tier 2 counts as reach, never as corroboration
# (tiers.py says so explicitly), which is why the tier sentence below separates
# the two.
TIER_MEANING = {
    1: "verified editorial standards",
    2: "national or regional outlet",
    3: "social, forums, unverified",
    4: "niche, hyperlocal, experimental",
}


def _tier_count(tier_mix: dict, tier: int) -> int:
    """How many outlets of a tier carried the story, from the badge's mix."""
    return int(tier_mix.get(f"t{tier}", 0) or 0)


def _tier_chips(tier_mix: dict) -> list:
    """One labelled chip per tier present: {"tier": 1, "count": 2, ...}.

    The chip text names the tier and counts its outlets in words, so nothing on
    a card is a bare digit with the meaning hidden in a tooltip that never
    appears on a phone. The meaning of the tier rides along as accessible text.
    """
    chips = []
    for tier in (1, 2, 3, 4):
        count = _tier_count(tier_mix, tier)
        if count <= 0:
            continue
        chips.append({
            "tier": tier,
            "count": count,
            "noun": "outlet" if count == 1 else "outlets",
            "meaning": TIER_MEANING[tier],
        })
    return chips


def _freshness(when, now: datetime) -> str:
    """How long ago something happened, in words: "12 minutes ago".

    Relative because "how stale is this" is the question a reader of a queue is
    actually asking, and an absolute date on its own does not answer it. The
    templates keep the machine-readable timestamp in the time element's
    datetime attribute, so nothing is lost by rendering this instead.
    """
    if when is None:
        return "timestamp not recorded"
    # SQLite hands back naive datetimes; the pipeline writes UTC, so a naive
    # value is UTC that has simply lost its marker.
    moment = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return "just now"
    for size, unit, limit in (
        (60, "minute", 60),
        (3600, "hour", 24),
        (86400, "day", 7),
        (604800, "week", 5),
    ):
        if seconds < size * limit:
            count = int(seconds // size) or 1
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return f"{int(seconds // 2592000)} months ago"


def _outlet_phrase(outlets: int) -> str:
    """A count with the noun it needs: 1 outlet, 2 outlets, no outlets."""
    if outlets == 0:
        return "no outlets"
    return "1 outlet" if outlets == 1 else f"{outlets} outlets"


def _tier_sentence(tier_mix: dict) -> str:
    """One sentence naming every tier present and what that tier means.

    Tier in words, never as a bare digit: "1 tier-1 outlet (verified editorial
    standards) and 2 tier-3 sources (social, forums, unverified)". A tier absent
    from the story is not mentioned, so the sentence stays readable.
    """
    present = [tier for tier in (1, 2, 3, 4) if _tier_count(tier_mix, tier) > 0]
    if not present:
        return "No sources have been attributed to this story yet."

    parts = []
    for tier in present:
        count = _tier_count(tier_mix, tier)
        noun = "outlet" if count == 1 else "outlets"
        parts.append(f"{count} tier-{tier} {noun} ({TIER_MEANING[tier]})")
    if len(parts) == 1:
        return parts[0].capitalize() + "."
    return ", ".join(parts[:-1]) + " and " + parts[-1] + "."


def _corroboration_sentence(verification: dict) -> str:
    """The badge's own verdict, restated as a sentence a person can act on.

    The wording follows discovery.py's _verification_badge labels so the number
    in the badge and the sentence on the card always agree.
    """
    outlets = int(verification.get("outlets", 0) or 0)
    if verification.get("corroborated"):
        return (
            f"Corroborated: two or more tier-1 outlets independently carried this "
            f"story, from {_outlet_phrase(outlets)}."
        )
    if outlets == 0:
        return "Not corroborated: no outlet has been attributed to this story yet."
    if outlets == 1:
        return (
            "Not corroborated: a single outlet carried this story, with no "
            "independent confirmation."
        )
    return (
        f"Not corroborated: {_outlet_phrase(outlets)} carried this story but not "
        f"two or more tier-1 outlets, so it still needs tier-1 confirmation."
    )


_STATUS_WORDS = {
    Story.Status.PENDING: "Awaiting review",
    Story.Status.QUEUED: "Queued for publishing",
    Story.Status.POSTED: "Published",
    Story.Status.REJECTED: "Rejected",
    Story.Status.BLOCKED: "Blocked by the gate",
    Story.Status.EXPIRED: "Expired",
}


def _status_sentence(story: Story) -> str:
    """The gate outcome as one human sentence.

    The gate writes its reason into gate_reason as free text, so it is quoted
    rather than reworded: the pipeline's own explanation is more trustworthy
    than a paraphrase of it.
    """
    status = Story.Status(story.status) if story.status else None
    words = _STATUS_WORDS.get(status, "Unknown")
    reason = (story.gate_reason or "").strip()
    if not reason:
        return f"{words}. No gate explanation was recorded."
    return f"{words}. Gate explanation: {reason}"


def _normalize_sort(sort: str | None) -> str:
    """An unknown sort falls back to the queue default rather than an error.

    The queue is a workbench, not an API: a mistyped sort should still show the
    queue, not a 500 page in front of the curator mid-triage.
    """
    return sort if sort in CURATION_SORTS else CURATION_SORT_DEFAULT


def _filter_query(
    tiers: list[int] | None = None,
    sort: str = CURATION_SORT_DEFAULT,
    hours: int | None = None,
    q: str | None = None,
) -> str:
    """The curator's active filters as a query string, carried into detail links.

    Defaults are omitted, so an unfiltered queue carries no query string at all
    and every non-default choice survives the round trip from a card to its
    detail page and back.
    """
    params: list[tuple[str, str]] = []
    if tiers and sorted(set(tiers)) != [1, 2, 3, 4]:
        params.append(("tiers", ",".join(str(t) for t in sorted(tiers))))
    if sort and sort != CURATION_SORT_DEFAULT:
        params.append(("sort", sort))
    try:
        window_hours = int(hours or 0)
    except (TypeError, ValueError):
        window_hours = 0
    if window_hours > 0:
        params.append(("hours", str(window_hours)))
    if q and q.strip():
        params.append(("q", q.strip()))
    return f"?{urlencode(params)}" if params else ""


def _queue_filter_state(
    tiers: list[int] | None,
    sort: str,
    hours: int | None,
    q: str | None,
) -> dict:
    """Template state for the discovery toolbar and the grid fragment.

    One place resolves the curator's selection into everything the templates
    need: the control values for the initial render, the same selection as a
    query string so a card's detail link returns to the same queue, and whether
    any filter is actually on, so an empty grid can say whether the queue is
    empty or the filters are.
    """
    try:
        window_hours = max(0, int(hours or 0))
    except (TypeError, ValueError):
        window_hours = 0
    active_tiers = tiers or [1, 2, 3, 4]
    active_sort = _normalize_sort(sort)
    active_q = (q or "").strip()
    return {
        "active_tiers": active_tiers,
        # All four tiers on is the unfiltered queue, so the hidden field carries
        # nothing: the same meaning always has one canonical query string.
        "active_tiers_csv": (
            "" if len(active_tiers) == 4 else ",".join(str(t) for t in active_tiers)
        ),
        "active_sort": active_sort,
        "active_hours": window_hours,
        "active_q": active_q,
        "filter_query": _filter_query(
            tiers=tiers, sort=active_sort, hours=window_hours, q=active_q
        ),
        "filters_active": bool(
            tiers
            or active_sort != CURATION_SORT_DEFAULT
            or window_hours > 0
            or active_q
        ),
    }


async def _render_stories_grid(
    session: AsyncSession,
    tiers: list[int] | None = None,
    sort: str = CURATION_SORT_DEFAULT,
    hours: int | None = None,
    q: str | None = None,
) -> list:
    """Render the stories grid fragment for HTMX swap.

    Discovery filters (tiers, sort, date window, topic search) come from the
    curator's controls and are applied with the same helpers the public map
    uses, so a tier means the same thing on both surfaces.

    Defense-in-depth: Filter out viewpoint sub-stories that don't have
    ≥2 distinct tier-1 owners. This ensures that even if a viewpoint
    sub-story somehow bypasses apply_tier1_gate, it won't appear in
    the curation queue.
    """
    search_ids: list | None = None
    if q and q.strip():
        search_ids = await _search_story_ids(session, q.strip(), limit=CURATION_SEARCH_LIMIT)
        if not search_ids:
            return []

    window_start, _window_end = _resolve_window(hours, datetime.now(timezone.utc))

    stmt = (
        select(Story)
        .where(Story.status == Story.Status.PENDING)
        # Exclude viewpoint sub-stories that lack tier-1 gate compliance
        .where(
            (Story.viewpoint_cluster_id.is_(None)) |  # Not a viewpoint sub-story
            ((Story.tier1_unit_count >= 2) & (Story.distinct_owners >= 2))  # Passes tier-1 gate
        )
        # A story is carried by an outlet when it has a unit in that tier, so
        # tiers=1,2 hides stories carried only by tier-3 outlets and keeps a
        # tier-1 story that also picked up tier-3 coverage.
        .options(
            selectinload(Story.units).selectinload(ReportingUnit.representative)
        )
    )

    tier_condition = _tier_include_condition(tiers)
    if tier_condition is not None:
        stmt = stmt.where(tier_condition)

    # The queue's own clock is when the story was created: the curator is
    # triaging what the pipeline produced, not when something happened on the
    # map. An unbounded window (0 or absent) adds no bound at all.
    if window_start is not None:
        stmt = stmt.where(Story.created_at >= window_start)

    if search_ids is not None:
        stmt = stmt.where(Story.id.in_(search_ids))

    order = {
        # "top" ranks by corroboration the same way the map does, then recency
        # to break ties between equally corroborated stories.
        "top": (
            desc(Story.distinct_owners),
            desc(Story.tier1_unit_count),
            desc(Story.day),
            desc(Story.created_at),
        ),
        "oldest": (asc(Story.day), asc(Story.created_at)),
        "newest": (desc(Story.day), desc(Story.created_at)),
    }[_normalize_sort(sort)]

    stmt = stmt.order_by(*order).limit(CURATION_QUEUE_LIMIT)

    result = await session.execute(stmt)
    stories = result.scalars().all()

    if not stories:
        return []

    filter_query = _filter_query(tiers=tiers, sort=_normalize_sort(sort), hours=hours, q=q)

    story_data = []
    now = datetime.now(timezone.utc)
    # Keep as UUID objects for the IN-clause bind params (the UUID column type
    # expects actual uuid.UUID instances, not strings); str() versions are used
    # below only as dict keys for grouping.
    story_ids = [s.id for s in stories]

    # One batched query for the whole queue. The card shows a lead image when the
    # story has one and nothing at all when it does not.
    media_result = await session.execute(
        select(MediaAsset)
        .where(MediaAsset.story_id.in_(story_ids))
        .order_by(MediaAsset.media_type, desc(MediaAsset.created_at), MediaAsset.id)
    )
    media_by_story: dict[str, list] = {}
    for media in media_result.scalars().all():
        media_by_story.setdefault(str(media.story_id), []).append(media)

    for story in stories:
        articles = _representative_articles(story.units)
        verification = _verification_badge(story)
        story_data.append({
            "story": story,
            "units": story.units,
            "articles": articles,
            "headline": _story_headline(articles),
            "media": _lead_media(media_by_story.get(str(story.id), [])),
            "lead_image": _lead_image(media_by_story.get(str(story.id), [])),
            "sources": _article_rows(articles),
            # The corroboration state, and the labelled tier chips that state it
            # visually. Both come from discovery.py's badge, so the card can never
            # disagree with the filter that selected it.
            "verification": verification,
            "tier_chips": _tier_chips(verification["tier_mix"]),
            "corroboration_sentence": _corroboration_sentence(verification),
            "freshness": _freshness(story.created_at, now),
            # Carried on the detail link so the queue survives the round trip.
            "filter_query": filter_query,
        })

    return story_data


def _representative_articles(units) -> list:
    """A story's representative articles, oldest first and undated last.

    The unit join carries no ordering, so this sort is what makes the headline
    pick deterministic instead of dependent on database row order.
    """
    articles = [unit.representative for unit in units if unit.representative]
    articles.sort(key=lambda article: (article.published_at is None, article.published_at))
    return articles


def _story_headline(articles: list) -> dict:
    """A story's headline, derived the way the public surfaces already derive it.

    The Story table has no headline column, so the repo's convention is to name a
    story after the representative article that first reported it, preferring an
    available translation. map_api.py and public_pages.py both already do exactly
    this, so a queue card and the public story page can never disagree about what
    a story is called.

    Returns the pieces both the card and the detail page need:

    headline          what to show, or NO_HEADLINE when nothing was captured
    headline_original the untranslated title, when it differs
    source_domain     which outlet the headline came from, for attribution
    translated        whether the shown headline is a translation
    """
    for article in articles:
        original = (article.title or "").strip()
        translated = (article.title_en or "").strip()
        title = translated or original
        if not title:
            continue
        return {
            "headline": title,
            "headline_original": original or title,
            "source_domain": article.source_domain,
            "translated": bool(translated) and translated != original,
        }
    return {
        "headline": NO_HEADLINE,
        "headline_original": "",
        "source_domain": None,
        "translated": False,
    }


def _story_summary(articles: list) -> dict | None:
    """The best available stand-in for a story body, and where it came from.

    The Story row has no body text either, so the detail page leads with the
    first reporting outlet's own summary, falling back to the opening of its
    body. Attribution is not optional here: this is one outlet's account of a
    story, not a neutral synopsis, and the page names the outlet it is quoting
    so a reader never mistakes it for something the pipeline composed.
    """
    for article in articles:
        raw = (article.summary or article.body_text or "").strip()
        if not raw:
            continue
        collapsed = " ".join(raw.split())
        return {
            "text": collapsed[:DETAIL_SUMMARY_CHARS],
            "truncated": len(collapsed) > DETAIL_SUMMARY_CHARS,
            "domain": article.source_domain or "an unattributed outlet",
            "url": article.url,
        }
    return None


def _lead_image(media_assets: list):
    """The image the card shows at the top, or None when the story has none.

    Cards are uneven heights by nature, so the grid reserves the space and lets
    the image fill it rather than pushing every other card down when one story
    happens to have artwork.
    """
    for asset in media_assets:
        if asset.media_type in ("image", "photo"):
            return asset
    return None


def _article_rows(articles: list) -> list:
    """One row per attributed source, pre-shaped for the templates.

    Domain, tier number and timestamp are resolved here rather than in Jinja so
    the card and the detail page read the same source list from the same place,
    and so an article with no domain or no tier degrades to a stated unknown
    instead of rendering a blank or a crash.
    """
    rows = []
    for article in articles:
        tier_num = None
        if article.source_tier:
            tier_num = {"tier1": 1, "tier2": 2, "tier3": 3, "tier4": 4}.get(
                article.source_tier.value
            )
        rows.append({
            "article": article,
            "domain": article.source_domain or "unknown domain",
            "tier_num": tier_num,
            "published_at": article.published_at,
            "url": article.url,
        })
    return rows


def _lead_media(media_assets: list) -> list:
    """At most one asset per media type, latest first, capped at 6.

    Deduplicating by type stops one article's three renditions of the same photo
    from filling the media strip with three copies of it.
    """
    by_type: dict = {}
    for asset in media_assets:
        by_type.setdefault(asset.media_type, asset)
        if len(by_type) >= 6:
            break
    return list(by_type.values())


async def _story_detail_bundle(session: AsyncSession, story: Story) -> dict:
    """Everything the detail page shows about one story.

    This is the single place the claim matrix, topic groups, narrative edges,
    source reliability scores and quoted snippets are still loaded. They left the
    card because they are diagnostics: useful on one page you opened on purpose,
    noise on fifty you scrolled past. The page files them under a disclosure.
    """
    story_id = story.id

    claim_result = await session.execute(select(Claim).where(Claim.story_id == story_id))
    claims = claim_result.scalars().all()

    evidence_by_claim: dict[str, list] = {}
    claim_ids = [claim.id for claim in claims]
    if claim_ids:
        evidence_result = await session.execute(
            select(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_ids))
        )
        for row in evidence_result.scalars().all():
            evidence_by_claim.setdefault(str(row.claim_id), []).append(row)

    topic_result = await session.execute(
        select(StoryTopicGroup).where(StoryTopicGroup.story_id == story_id)
    )
    topic_links = topic_result.scalars().all()

    # Topic groups are named in their own table, and a bare uuid in a disclosure
    # tells a reader nothing, so the names come along.
    topics = []
    group_ids = [link.topic_group_id for link in topic_links]
    if group_ids:
        group_result = await session.execute(
            select(TopicGroup).where(TopicGroup.id.in_(group_ids))
        )
        name_by_id = {group.id: group.name for group in group_result.scalars().all()}
    else:
        name_by_id = {}
    for link in topic_links:
        topics.append({
            "name": name_by_id.get(link.topic_group_id, "unnamed topic"),
            "confidence": link.confidence,
        })

    # Narrative arcs, with this story as the subject: the links out to the
    # stories it turns out to be the same event as, or a step within.
    edge_result = await session.execute(
        select(EntityEdge)
        .where(EntityEdge.subject_type == "story")
        .where(EntityEdge.subject_id == story_id)
        .where(
            EntityEdge.predicate.in_(
                [EdgePredicate.SAME_EVENT_AS, EdgePredicate.PART_OF_NARRATIVE]
            )
        )
    )
    edges = list(edge_result.scalars().all())

    # Name the stories these edges point at, for the same reason: "the same event
    # as <headline>" is readable, "SAME_EVENT_AS 3f2a…-…" is not.
    target_ids = [edge.object_id for edge in edges if edge.object_type == "story"]
    target_headlines: dict = {}
    if target_ids:
        target_result = await session.execute(
            select(Story)
            .where(Story.id.in_(target_ids))
            .options(selectinload(Story.units).selectinload(ReportingUnit.representative))
        )
        for target in target_result.scalars().all():
            target_headlines[target.id] = _story_headline(
                _representative_articles(target.units)
            )["headline"]

    narrative_arcs = [
        {
            "predicate": edge.predicate.value if edge.predicate else "related to",
            "object_type": edge.object_type,
            "object_id": edge.object_id,
            "object_headline": target_headlines.get(edge.object_id),
        }
        for edge in edges
    ]

    snippet_result = await session.execute(
        select(Snippet).where(Snippet.story_id == story_id).order_by(desc(Snippet.confidence))
    )
    snippets = list(snippet_result.scalars().all())[:DETAIL_SNIPPET_CAP]

    media_result = await session.execute(
        select(MediaAsset)
        .where(MediaAsset.story_id == story_id)
        .order_by(desc(MediaAsset.created_at), MediaAsset.id)
    )
    media = _lead_media(list(media_result.scalars().all()))

    # Latest reliability score per domain behind this story.
    source_articles = await _story_source_articles(session, story_id)
    domains = {a.source_domain for a in source_articles if a.source_domain}
    reliability_by_domain: dict[str, float] = {}
    if domains:
        reliability_result = await session.execute(
            select(SourceReliabilitySnapshot)
            .where(SourceReliabilitySnapshot.source_domain.in_(domains))
            .order_by(
                SourceReliabilitySnapshot.source_domain,
                desc(SourceReliabilitySnapshot.snapshot_date),
            )
        )
        for snapshot in reliability_result.scalars().all():
            if snapshot.source_domain not in reliability_by_domain:
                reliability_by_domain[snapshot.source_domain] = snapshot.reliability_score

    return {
        "source_articles": source_articles,
        "media": media,
        "claims": [
            {"claim": claim, "evidence": evidence_by_claim.get(str(claim.id), [])}
            for claim in claims
        ],
        "topics": topics,
        "narrative_arcs": narrative_arcs,
        "snippets": snippets,
        "reliability_by_domain": reliability_by_domain,
        "primary_entity_names": await _primary_entity_names(session, story),
    }


async def _primary_entity_names(session: AsyncSession, story: Story) -> list[str]:
    """The story's primary entities as names, never as canonical ids.

    `Story.primary_entities` is a JSON array of canonical entity UUIDs (see
    `src/verification/stories.py`). A raw uuid in the markup is exactly the debug
    spew this page was rebuilt to remove, and the curation card has a standing
    regression test asserting entity ids never reach it, so the detail view
    resolves them the same way `src/verification/topics.py` does and drops the
    ones with no canonical row.
    """
    raw = story.primary_entities or []
    if isinstance(raw, dict):  # observed on some dev rows; treat as empty
        return []
    ids: list[uuid.UUID] = []
    names: list[str] = []
    for entry in raw:
        try:
            ids.append(uuid.UUID(str(entry)))
        except (AttributeError, TypeError, ValueError):
            # Legacy surface-form data passes straight through.
            names.append(str(entry))
    if not ids:
        return names
    result = await session.execute(
        select(CanonicalEntity.canonical_name).where(CanonicalEntity.id.in_(ids))
    )
    return names + list(result.scalars().all())


async def _story_source_articles(session: AsyncSession, story_id: uuid.UUID):
    """Every source article linked to a story, through its reporting units."""
    stmt = (
        select(RawArticle)
        .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.story_id == story_id)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def _get_story_or_404(session: AsyncSession, story_id: uuid.UUID) -> Story:
    """The story with this id, or 404 when no such id exists.

    No status filter, because nothing here acts on the story: the detail page
    has to stay readable for a story that has moved on from PENDING, which is
    the main reason to open it.
    """
    stmt = select(Story).where(Story.id == story_id)
    result = await session.execute(stmt)
    story = result.scalar_one_or_none()
    if not story:
        raise HTTPException(404, "Story not found")
    return story


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    tiers: str = None,
    sort: str = CURATION_SORT_DEFAULT,
    hours: int = 0,
    q: str = None,
    user: str = Depends(require_auth),
):
    """Show pending stories, filtered by the discovery controls.

    The controls (tier chips, date window, sort, topic search) are rendered
    server side in their initial state from this query string, so first paint
    already shows the filtered queue and the page is useful with no JavaScript
    at all.
    """
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    active_tiers = _parse_tiers(tiers)
    active_sort = _normalize_sort(sort)
    filter_state = _queue_filter_state(active_tiers, active_sort, hours, q)

    async with get_session() as session:
        stories = await _render_stories_grid(
            session,
            tiers=active_tiers,
            sort=active_sort,
            hours=hours,
            q=q,
        )

    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "stories": stories,
        **filter_state,
    })


@router.get("/story/{story_id}", response_class=HTMLResponse)
async def story_detail(
    story_id: uuid.UUID,
    request: Request,
    tiers: str = None,
    sort: str = CURATION_SORT_DEFAULT,
    hours: int = 0,
    q: str = None,
    user: str = Depends(require_auth),
):
    """Everything known about one story, on one page.

    Read-only and behind require_auth, like the queue: story internals are not
    public. The discovery filters are accepted only so the "back to the queue"
    link can restore the exact queue the reader came from.
    """
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    units = None
    articles = None
    async with get_session() as session:
        story = await _get_story_or_404(session, story_id)
        units = await _load_units_with_representatives(session, story_id)
        articles = _representative_articles(units)
        bundle = await _story_detail_bundle(session, story)

    verification = _verification_badge(story)
    back_query = _filter_query(
        tiers=_parse_tiers(tiers),
        sort=_normalize_sort(sort),
        hours=hours,
        q=q,
    )

    return templates.TemplateResponse(request, "story_detail.html", {
        "request": request,
        "story": story,
        "headline": _story_headline(articles),
        "articles": articles,
        "sources": _article_rows(articles),
        "lead_image": _lead_image(bundle["media"]),
        "summary": _story_summary(articles),
        "units": units,
        "verification": verification,
        "tier_chips": _tier_chips(verification["tier_mix"]),
        "tier_sentence": _tier_sentence(verification["tier_mix"]),
        "corroboration_sentence": _corroboration_sentence(verification),
        "status_sentence": _status_sentence(story),
        "freshness": _freshness(story.created_at, datetime.now(timezone.utc)),
        "back_query": back_query,
        **bundle,
    })


async def _load_units_with_representatives(session: AsyncSession, story_id) -> list:
    """This story's units, each with its representative article attached.

    The detail page needs the units in their own right (it reports how many
    reporting units and owners a story has) and needs the representative articles
    for the headline and the source list, so it loads both in one statement
    rather than reaching for the units relationship the queue route has already
    populated.
    """
    stmt = (
        select(ReportingUnit)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.story_id == story_id)
        .options(selectinload(ReportingUnit.representative))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())

