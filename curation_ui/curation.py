"""The curator's triage workbench: the story queue and the curated post list.

Every route here is behind require_auth, and the four that change state are also
behind require_csrf. The pages are rendered server side for HTMX swaps, so the
grid fragment (_render_stories_grid) is the unit of work shared by the queue page
and each mutating route's response.
"""

import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from curation_ui.app_state import check_database, check_llm, render_error_page, templates
from curation_ui.discovery import (
    _parse_tiers,
    _resolve_window,
    _search_story_ids,
    _tier_include_condition,
    _verification_badge,
)
from curation_ui.security import issue_csrf_token, require_auth, require_csrf
from src.schema.models import (
    Claim,
    ClaimEvidence,
    CuratedPost,
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
)
from src.shared.database import get_session
from src.shared.llm import build_deterministic_caption, get_llm_client, validate_caption

router = APIRouter()

logger = logging.getLogger(__name__)

# Curator facing copy for the two caption paths
NO_LLM_NOTICE = (
    "AI assistance is unavailable because no LLM is configured. "
    "Write your caption manually."
)
APPROVE_NO_LLM_NOTICE = (
    "AI assistance is unavailable because no LLM is configured. "
    "This caption is a deterministic draft, please review and edit it."
)
APPROVE_AI_NOTICE = "Caption generated with AI assistance."

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
    """The curator's active filters as a query string, for the card buttons.

    Defaults are omitted, so an unfiltered queue carries no query string at all
    and every non-default choice survives the HTMX re-render that follows an
    approve or a reject.
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
    query string for the card buttons, and whether any filter is actually on, so
    an empty grid can say whether the queue is empty or the filters are.
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
    # Keep as UUID objects for the IN-clause bind params (the UUID column type
    # expects actual uuid.UUID instances, not strings); str() versions are used
    # below only as dict keys for grouping.
    story_ids = [s.id for s in stories]

    # Batch fetch MediaAssets for all stories
    media_stmt = (
        select(MediaAsset)
        .where(MediaAsset.story_id.in_(story_ids))
        .order_by(MediaAsset.media_type, desc(MediaAsset.created_at))
    )
    media_result = await session.execute(media_stmt)
    media_assets = media_result.scalars().all()

    # Group media by story_id
    media_by_story = {}
    for media in media_assets:
        story_id = str(media.story_id)
        if story_id not in media_by_story:
            media_by_story[story_id] = []
        media_by_story[story_id].append(media)

    # Batch fetch Snippets for all stories
    snippet_stmt = (
        select(Snippet)
        .where(Snippet.story_id.in_(story_ids))
        .order_by(desc(Snippet.confidence))
    )
    snippet_result = await session.execute(snippet_stmt)
    snippets = snippet_result.scalars().all()

    # Group snippets by story_id
    snippets_by_story = {}
    for snippet in snippets:
        story_id = str(snippet.story_id)
        if story_id not in snippets_by_story:
            snippets_by_story[story_id] = []
        snippets_by_story[story_id].append(snippet)

    # Batch fetch reliability scores for all source domains across stories
    # First, collect all unique source domains
    all_domains = set()
    for story in stories:
        units = story.units
        articles = [unit.representative for unit in units if unit.representative]
        for article in articles:
            if article.source_domain:
                all_domains.add(article.source_domain)

    if all_domains:
        # Get latest reliability snapshot per domain
        reliability_stmt = (
            select(SourceReliabilitySnapshot)
            .where(SourceReliabilitySnapshot.source_domain.in_(all_domains))
            .order_by(SourceReliabilitySnapshot.source_domain, desc(SourceReliabilitySnapshot.snapshot_date))
        )
        reliability_result = await session.execute(reliability_stmt)
        reliability_snapshots = reliability_result.scalars().all()

        # Keep only the most recent per domain
        reliability_by_domain = {}
        for snap in reliability_snapshots:
            if snap.source_domain not in reliability_by_domain:
                reliability_by_domain[snap.source_domain] = snap.reliability_score
    else:
        reliability_by_domain = {}

    # Batch fetch Claims for all stories
    claim_stmt = (
        select(Claim)
        .where(Claim.story_id.in_(story_ids))
    )
    claim_result = await session.execute(claim_stmt)
    claims = claim_result.scalars().all()

    # Group claims by story_id
    claims_by_story = {}
    for claim in claims:
        story_id = str(claim.story_id)
        if story_id not in claims_by_story:
            claims_by_story[story_id] = []
        claims_by_story[story_id].append(claim)

    # Batch fetch ClaimEvidence for all claims
    claim_ids = [c.id for c in claims]
    if claim_ids:
        evidence_stmt = (
            select(ClaimEvidence)
            .where(ClaimEvidence.claim_id.in_(claim_ids))
        )
        evidence_result = await session.execute(evidence_stmt)
        evidence_items = evidence_result.scalars().all()

        # Group evidence by claim_id
        evidence_by_claim = {}
        for ev in evidence_items:
            claim_id = str(ev.claim_id)
            if claim_id not in evidence_by_claim:
                evidence_by_claim[claim_id] = []
            evidence_by_claim[claim_id].append(ev)
    else:
        evidence_by_claim = {}

    # Batch fetch StoryTopicGroup for all stories
    topic_stmt = (
        select(StoryTopicGroup)
        .where(StoryTopicGroup.story_id.in_(story_ids))
    )
    topic_result = await session.execute(topic_stmt)
    topic_links = topic_result.scalars().all()

    # Group topic groups by story_id
    topics_by_story = {}
    for link in topic_links:
        story_id = str(link.story_id)
        if story_id not in topics_by_story:
            topics_by_story[story_id] = []
        topics_by_story[story_id].append(link)

    # Batch fetch EntityEdges (narrative arcs) for all stories
    # We want edges where this story is the subject (newer story linking to older)
    edge_stmt = (
        select(EntityEdge)
        .where(EntityEdge.subject_type == "story")
        .where(EntityEdge.subject_id.in_(story_ids))
        .where(EntityEdge.predicate.in_([EdgePredicate.SAME_EVENT_AS, EdgePredicate.PART_OF_NARRATIVE]))
    )
    edge_result = await session.execute(edge_stmt)
    edges = edge_result.scalars().all()

    # Group edges by subject story_id
    edges_by_story = {}
    for edge in edges:
        story_id = str(edge.subject_id)
        if story_id not in edges_by_story:
            edges_by_story[story_id] = []
        edges_by_story[story_id].append(edge)

    for story in stories:
        units = story.units
        articles = [unit.representative for unit in units if unit.representative]

        # Get media for this story (cap at ~6, prefer one per type)
        story_media = media_by_story.get(str(story.id), [])
        # Deduplicate by media_type, preferring latest
        seen_types = set()
        filtered_media = []
        for m in story_media:
            if m.media_type not in seen_types:
                filtered_media.append(m)
                seen_types.add(m.media_type)
            if len(filtered_media) >= 6:
                break

        # Get snippets for this story (cap at 3)
        story_snippets = snippets_by_story.get(str(story.id), [])[:3]

        # Get claims for this story with evidence
        story_claims = claims_by_story.get(str(story.id), [])
        claims_with_evidence = []
        for claim in story_claims:
            ev = evidence_by_claim.get(str(claim.id), [])
            claims_with_evidence.append({
                "claim": claim,
                "evidence": ev,
            })

        # Get topic groups for this story
        story_topics = topics_by_story.get(str(story.id), [])

        # Get narrative arcs for this story
        story_edges = edges_by_story.get(str(story.id), [])

        # Build reliability map for this story's sources
        story_reliability = {}
        for article in articles:
            if article.source_domain and article.source_domain in reliability_by_domain:
                story_reliability[article.source_domain] = reliability_by_domain[article.source_domain]

        story_data.append({
            "story": story,
            "units": units,
            "articles": articles,
            "media": filtered_media,
            "snippets": story_snippets,
            "reliability_by_domain": story_reliability,
            "claims": claims_with_evidence,
            "topic_groups": story_topics,
            "narrative_arcs": story_edges,
            # The corroboration state a curator triages on, and the filters to
            # carry into the approve/reject re-render.
            "verification": _verification_badge(story),
            "filter_query": filter_query,
        })

    return story_data


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
    """The story this action applies to, or 404 when no such id exists.

    No status filter: the PENDING precondition is enforced by the callers that
    need it (approve, save), each of which raises 409 with the current status.
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
    """Show pending stories for triage, filtered by the curator's discovery controls.

    The controls (tier chips, date window, sort, topic search) are rendered
    server side in their initial state from this query string, so first paint
    already shows the filtered queue and the workbench is useful with no
    JavaScript at all.
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
        # Count approved posts for the header link
        stmt = select(CuratedPost).where(CuratedPost.status == CuratedPost.Status.APPROVED)
        result = await session.execute(stmt)
        posts = result.scalars().all()
        posts_count = len(posts)

    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "stories": stories,
        "posts_count": posts_count,
        "csrf_token": issue_csrf_token(),
        **filter_state,
    })


@router.post("/story/{story_id}/approve")
async def approve_story(
    story_id: uuid.UUID,
    request: Request,
    tiers: str = None,
    sort: str = CURATION_SORT_DEFAULT,
    hours: int = 0,
    q: str = None,
    user: str = Depends(require_auth),
    csrf: None = Depends(require_csrf),
):
    """Approve a story for posting - creates a CuratedPost and queues the story.

    The caption comes from the LLM when one is configured, otherwise from the
    deterministic builder, so approval works with no provider key at all.

    The discovery filters are accepted on the query string the card button sends
    so the returned grid re-renders the queue the curator is actually looking at.
    """
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    llm_ok, _ = check_llm(request)

    active_tiers = _parse_tiers(tiers)
    active_sort = _normalize_sort(sort)
    filter_state = _queue_filter_state(active_tiers, active_sort, hours, q)

    async with get_session() as session:
        story = await _get_story_or_404(session, story_id)

        # Only allow approval of PENDING stories
        if story.status != Story.Status.PENDING:
            raise HTTPException(409, f"Story is not in PENDING status (current: {story.status.value})")

        # Check if CuratedPost already exists for this story (idempotency)
        existing_post_stmt = select(CuratedPost).where(CuratedPost.story_id == story_id)
        existing_post_result = await session.execute(existing_post_stmt)
        existing_post = existing_post_result.scalar_one_or_none()
        if existing_post:
            raise HTTPException(409, f"Story already has a curated post (id: {existing_post.id})")

        # Get source URLs and key facts for caption generation
        articles = await _story_source_articles(session, story_id)
        source_urls = [a.url for a in articles]
        key_facts = [a.title for a in articles[:3]]

        # Fetch media for this story
        media_stmt = (
            select(MediaAsset)
            .where(MediaAsset.story_id == story_id)
            .order_by(MediaAsset.media_type, desc(MediaAsset.created_at))
        )
        media_result = await session.execute(media_stmt)
        media_assets = media_result.scalars().all()

        # Build media_urls for CuratedPost (cap at 3, prefer lead image + video)
        media_urls = []
        image_count = 0
        video_count = 0
        for asset in media_assets:
            if asset.media_type.value == "image" and image_count == 0:
                media_urls.append({
                    "type": "image",
                    "url": asset.url,
                    "alt": asset.alt_text or ""
                })
                image_count += 1
            elif asset.media_type.value == "video" and video_count == 0:
                media_urls.append({
                    "type": "video",
                    "url": asset.url,
                    "alt": asset.alt_text or asset.source or "Video"
                })
                video_count += 1
            elif len(media_urls) < 3 and asset.media_type.value in ("image", "video", "embed"):
                media_urls.append({
                    "type": asset.media_type.value,
                    "url": asset.url,
                    "alt": asset.alt_text or asset.source or asset.media_type.value
                })
            if len(media_urls) >= 3:
                break

        # Fetch top snippets for this story (cap at 3)
        snippet_stmt = (
            select(Snippet)
            .where(Snippet.story_id == story_id)
            .order_by(desc(Snippet.confidence))
            .limit(3)
        )
        snippet_result = await session.execute(snippet_stmt)
        snippets = snippet_result.scalars().all()

        # Add snippet texts to key_facts for richer caption generation
        for snippet in snippets:
            if snippet.text:
                key_facts.append(snippet.text[:300])

        # Generate the caption: LLM when configured, deterministic draft otherwise
        story_title = "; ".join(key_facts) if key_facts else "News Update"
        ai_assisted = False
        if llm_ok:
            llm = await get_llm_client()
            caption = await llm.generate_caption(
                story_title=story_title,
                key_facts=key_facts,
                source_urls=source_urls,
                platform="twitter",
            )
            if not caption:
                raise HTTPException(500, "Failed to generate caption")
            ai_assisted = True
        else:
            caption = build_deterministic_caption(
                story_title=story_title,
                key_facts=key_facts,
                source_urls=source_urls,
                platform="twitter",
            )
            logger.info("No LLM configured, approved story %s with a deterministic caption draft", story_id)

        # Create curated post with media_urls
        post = CuratedPost(
            story_id=story_id,
            platform="twitter",
            caption=caption,
            media_urls=media_urls if media_urls else None,
            source_urls=source_urls,
            status=CuratedPost.Status.APPROVED,
        )
        session.add(post)
        story.status = Story.Status.QUEUED
        story.updated_at = datetime.now(timezone.utc)
        await session.commit()

        # Return updated stories grid fragment
        stories = await _render_stories_grid(
            session,
            tiers=active_tiers,
            sort=active_sort,
            hours=hours,
            q=q,
        )

    return templates.TemplateResponse(request, "story_grid.html", {
        "request": request,
        "stories": stories,
        "notice": (
            APPROVE_AI_NOTICE if ai_assisted else APPROVE_NO_LLM_NOTICE
        ),
        "notice_type": "info" if ai_assisted else "warning",
        **filter_state,
    })


@router.post("/story/{story_id}/reject")
async def reject_story(
    story_id: uuid.UUID,
    request: Request,
    tiers: str = None,
    sort: str = CURATION_SORT_DEFAULT,
    hours: int = 0,
    q: str = None,
    user: str = Depends(require_auth),
    csrf: None = Depends(require_csrf),
):
    """Reject a story - returns stories grid fragment for HTMX.

    Carries the curator's discovery filters on the query string, exactly like
    approve, so the re-rendered grid stays inside the active filters.
    """
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    active_tiers = _parse_tiers(tiers)
    active_sort = _normalize_sort(sort)
    filter_state = _queue_filter_state(active_tiers, active_sort, hours, q)

    async with get_session() as session:
        story = await _get_story_or_404(session, story_id)

        story.status = Story.Status.REJECTED
        story.updated_at = datetime.now(timezone.utc)
        await session.commit()

        # Return updated stories grid fragment
        stories = await _render_stories_grid(
            session,
            tiers=active_tiers,
            sort=active_sort,
            hours=hours,
            q=q,
        )

    return templates.TemplateResponse(request, "story_grid.html", {
        "request": request,
        "stories": stories,
        **filter_state,
    })


@router.get("/story/{story_id}/edit", response_class=HTMLResponse)
async def edit_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Show edit form for a story.

    The draft is prefilled from text already saved for the story. Otherwise it
    comes from the LLM when one is configured, and stays blank when none is, so
    the curator can write the caption by hand.
    """
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    llm_ok, _ = check_llm(request)

    async with get_session() as session:
        story = await _get_story_or_404(session, story_id)

        # Get source URLs
        articles = await _story_source_articles(session, story_id)

        source_urls = [a.url for a in articles]
        key_facts = [a.title for a in articles[:3]]

        # Existing caption for this story, so an edit never starts blank
        existing_stmt = (
            select(CuratedPost)
            .where(CuratedPost.story_id == story_id)
            .order_by(desc(CuratedPost.created_at))
        )
        existing_result = await session.execute(existing_stmt)
        existing_post = existing_result.scalars().first()
        existing_caption = str(existing_post.caption or "") if existing_post else ""

        # Generate a draft caption only when there is no saved one to prefill
        caption: str = existing_caption
        ai_assisted = False
        if not caption and llm_ok:
            llm = await get_llm_client()
            caption = await llm.generate_caption(
                story_title="; ".join(key_facts) if key_facts else "News Update",
                key_facts=key_facts,
                source_urls=source_urls,
                platform="twitter",
            ) or ""
            ai_assisted = bool(caption)

        # Fetch media for this story
        media_stmt = (
            select(MediaAsset)
            .where(MediaAsset.story_id == story_id)
            .order_by(MediaAsset.media_type, desc(MediaAsset.created_at))
        )
        media_result = await session.execute(media_stmt)
        media = media_result.scalars().all()

        # Deduplicate by media_type, preferring latest
        seen_types = set()
        filtered_media = []
        for m in media:
            if m.media_type not in seen_types:
                filtered_media.append(m)
                seen_types.add(m.media_type)
            if len(filtered_media) >= 6:
                break

        # Fetch snippets for this story
        snippet_stmt = (
            select(Snippet)
            .where(Snippet.story_id == story_id)
            .order_by(desc(Snippet.confidence))
            .limit(3)
        )
        snippet_result = await session.execute(snippet_stmt)
        snippets = snippet_result.scalars().all()

        # Fetch reliability scores for this story's sources
        reliability_by_domain = {}
        for article in articles:
            if article.source_domain:
                rel_stmt = (
                    select(SourceReliabilitySnapshot)
                    .where(SourceReliabilitySnapshot.source_domain == article.source_domain)
                    .order_by(desc(SourceReliabilitySnapshot.snapshot_date))
                    .limit(1)
                )
                rel_result = await session.execute(rel_stmt)
                snap = rel_result.scalar_one_or_none()
                if snap:
                    reliability_by_domain[article.source_domain] = snap.reliability_score

    return templates.TemplateResponse(request, "edit.html", {
        "request": request,
        "story": story,
        "articles": articles,
        "source_urls": source_urls,
        "key_facts": key_facts,
        "draft_caption": caption or "",
        "llm_available": llm_ok,
        "ai_assisted": ai_assisted,
        "no_llm_notice": NO_LLM_NOTICE,
        "media": filtered_media,
        "snippets": snippets,
        "reliability_by_domain": reliability_by_domain,
        "csrf_token": issue_csrf_token(),
    })


@router.post("/story/{story_id}/save")
async def save_story(
    story_id: uuid.UUID,
    request: Request,
    caption: str = Form(...),
    platform: str = Form("twitter"),
    override_validation: bool = Form(False),  # Allow manual override
    user: str = Depends(require_auth),
    csrf: None = Depends(require_csrf),
):
    """Save edited story as curated post.

    The caption always comes from the form, so this path never calls an LLM,
    with or without a configured provider.
    """
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        story = await _get_story_or_404(session, story_id)

        # Only allow saving of PENDING stories
        if story.status != Story.Status.PENDING:
            raise HTTPException(409, f"Story is not in PENDING status (current: {story.status.value})")

        # Get source articles for validation
        articles = await _story_source_articles(session, story_id)
        source_urls = [a.url for a in articles]

        # Validate caption server-side (warning only for manual override)
        # Use all linked article titles + summaries + body texts for paraphrase detection
        source_texts = []
        for a in articles:
            source_texts.append(a.title)
            if a.summary:
                source_texts.append(a.summary)
            if a.body_text:
                source_texts.append(a.body_text)
        is_valid, error = validate_caption(caption, platform, source_texts, allow_override=override_validation)
        if not is_valid and not override_validation:
            logger.warning("Caption validation failed: %s", error)
            return templates.TemplateResponse(request, "error.html", {
                "request": request,
                "message": f"Caption validation failed: {error} (check 'Override validation' to save anyway)",
            }, status_code=400)
        elif not is_valid and override_validation:
            logger.warning("Caption validation overridden by user: %s", error)

        # Create curated post
        post = CuratedPost(
            story_id=story_id,
            platform=platform,
            caption=caption,
            source_urls=source_urls,
            status=CuratedPost.Status.APPROVED,
        )
        session.add(post)
        story.status = Story.Status.QUEUED
        story.updated_at = datetime.now(timezone.utc)
        await session.commit()

    return RedirectResponse(url="/", status_code=303)


@router.get("/posts", response_class=HTMLResponse)
async def list_posts(request: Request, user: str = Depends(require_auth)):
    """Show approved posts ready for publishing."""
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = (
            select(CuratedPost)
            .where(CuratedPost.status == CuratedPost.Status.APPROVED)
            .order_by(desc(CuratedPost.created_at))
        )
        result = await session.execute(stmt)
        posts = result.scalars().all()

    return templates.TemplateResponse(request, "posts.html", {
        "request": request,
        "posts": posts,
        "csrf_token": issue_csrf_token(),
    })


@router.post("/post/{post_id}/mark-posted")
async def mark_posted(
    post_id: uuid.UUID,
    request: Request,
    user: str = Depends(require_auth),
    csrf: None = Depends(require_csrf),
):
    """Mark a post as published."""
    db_ok, db_msg = check_database(request)
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = select(CuratedPost).where(CuratedPost.id == post_id)
        result = await session.execute(stmt)
        post = result.scalar_one_or_none()
        if not post:
            raise HTTPException(404, "Post not found")

        post.status = CuratedPost.Status.POSTED
        post.posted_at = datetime.now(timezone.utc)
        await session.commit()

    return RedirectResponse(url="/posts", status_code=303)