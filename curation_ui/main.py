"""FastAPI + HTMX curation UI for story triage."""

import logging
import socket
import secrets
import time
from collections import defaultdict
from typing import Dict, List, Tuple

# Force IPv4-only DNS resolution to avoid Vercel's lack of outbound IPv6 routes
# This patches the resolver asyncio (and asyncpg through it) calls underneath
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

from fastapi import FastAPI, Request, Form, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select, desc, and_, func
from sqlalchemy.types import Float
from sqlalchemy.ext.asyncio import AsyncSession
from src.shared.database import get_session
from src.schema.models import Story, StoryUnitLink, ReportingUnit, RawArticle, CuratedPost, Event, EventLayer, Claim, ClaimEvidence, StoryTopicGroup, EntityEdge, EdgePredicate
from src.shared.llm import get_llm_client, validate_caption, build_deterministic_caption
from src.shared.config import get_settings
from curation_ui.health import router as health_router
from datetime import datetime, timedelta, timezone
import uuid
import os


# Get the directory where this file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="News Pipeline Curation")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

settings = get_settings()

logger = logging.getLogger(__name__)

app.include_router(health_router)

security = HTTPBasic()


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


# In-memory rate limiter for FAILED auth attempts only
# Stores (timestamp, is_failed) tuples per client IP
_auth_attempts: Dict[str, List[Tuple[float, bool]]] = defaultdict(list)
_AUTH_WINDOW_SECONDS = 60  # 1 minute window
_AUTH_MAX_FAILED = 10  # Max failed attempts per window


def _get_client_ip(request: Request) -> str:
    """Extract client IP, honoring X-Forwarded-For header."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Take the first IP in the chain (original client)
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _clean_old_attempts(ip: str, now: float) -> None:
    """Remove attempts older than the window."""
    cutoff = now - _AUTH_WINDOW_SECONDS
    _auth_attempts[ip] = [(ts, failed) for ts, failed in _auth_attempts[ip] if ts > cutoff]


def _count_failed_attempts(ip: str, now: float) -> int:
    """Count failed attempts in the current window."""
    _clean_old_attempts(ip, now)
    return sum(1 for ts, failed in _auth_attempts[ip] if failed)


def _record_attempt(ip: str, success: bool) -> None:
    """Record an auth attempt."""
    now = time.time()
    _auth_attempts[ip].append((now, not success))  # Store True for failed


async def require_auth(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> str:
    """Require HTTP Basic auth for all mutating endpoints.

    Rate limits only FAILED attempts (max 10 per minute per IP).
    Successful attempts are not counted against the limit.
    """
    if not settings.has_curation_auth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Curation UI not configured: CURATION_USER and CURATION_PASSWORD must be set",
        )

    client_ip = _get_client_ip(request)
    now = time.time()

    ok_user = secrets.compare_digest(creds.username, settings.curation_user)
    ok_pass = secrets.compare_digest(creds.password, settings.curation_password)

    if not (ok_user and ok_pass):
        # Check rate limit for failed attempts BEFORE recording this one
        failed_count = _count_failed_attempts(client_ip, now)
        if failed_count >= _AUTH_MAX_FAILED:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed authentication attempts. Try again in {_AUTH_WINDOW_SECONDS} seconds.",
            )
        _record_attempt(client_ip, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Record successful attempt (doesn't count toward limit)
    _record_attempt(client_ip, success=True)
    return creds.username


def check_database_available() -> tuple[bool, str]:
    """Check if database is available, return (available, error_message)."""
    if not settings.has_database:
        return False, "Database not configured. Set DATABASE_URL environment variable."
    return True, ""


def check_llm_available() -> tuple[bool, str]:
    """Check if LLM is available, return (available, error_message).

    Availability is either a configured provider API key, or an already
    -initialized/injected client (e.g. the mock LLMClient tests set on
    src.shared.llm._llm_client). Gating on settings.has_llm alone made this
    return False even when a working client was already in place.

    A False result is not fatal: approve and edit fall back to deterministic,
    LLM-free behavior (see build_deterministic_caption) and tell the curator.
    """
    import src.shared.llm as llm_module
    if not settings.has_llm and llm_module._llm_client is None:
        return False, "No LLM configured. Set GROQ_API_KEY or CEREBRAS_API_KEY environment variable."
    return True, ""


def render_error_page(request: Request, message: str, status_code: int = 503) -> HTMLResponse:
    """Render a friendly error page when a feature is unavailable."""
    return templates.TemplateResponse(
        request,
        "error.html",
        {"request": request, "message": message},
        status_code=status_code
    )


async def _render_stories_grid(session: AsyncSession) -> list:
    """Render the stories grid fragment for HTMX swap.

    Defense-in-depth: Filter out viewpoint sub-stories that don't have
    ≥2 distinct tier-1 owners. This ensures that even if a viewpoint
    sub-story somehow bypasses apply_tier1_gate, it won't appear in
    the curation queue.
    """
    from sqlalchemy import select, desc
    from sqlalchemy.orm import selectinload
    from src.schema.models import (
        Story, ReportingUnit, MediaAsset, Snippet, SourceReliabilitySnapshot
    )

    stmt = (
        select(Story)
        .where(Story.status == Story.Status.PENDING)
        # Exclude viewpoint sub-stories that lack tier-1 gate compliance
        .where(
            (Story.viewpoint_cluster_id.is_(None)) |  # Not a viewpoint sub-story
            ((Story.tier1_unit_count >= 2) & (Story.distinct_owners >= 2))  # Passes tier-1 gate
        )
        .order_by(desc(Story.day), desc(Story.created_at))
        .limit(50)
        .options(
            selectinload(Story.units).selectinload(ReportingUnit.representative)
        )
    )
    result = await session.execute(stmt)
    stories = result.scalars().all()

    if not stories:
        return []

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
        })

    return story_data


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(require_auth)):
    """Show pending stories for triage."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stories = await _render_stories_grid(session)
        # Count approved posts for the header link
        stmt = select(CuratedPost).where(CuratedPost.status == CuratedPost.Status.APPROVED)
        result = await session.execute(stmt)
        posts = result.scalars().all()
        posts_count = len(posts)

    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "stories": stories,
        "posts_count": posts_count,
    })


@app.post("/story/{story_id}/approve")
async def approve_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Approve a story for posting - creates a CuratedPost and queues the story.

    The caption comes from the LLM when one is configured, otherwise from the
    deterministic builder, so approval works with no provider key at all.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    llm_ok, _ = check_llm_available()

    from src.schema.models import MediaAsset, Snippet
    from sqlalchemy import select, desc

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        # Only allow approval of PENDING stories
        if story.status != Story.Status.PENDING:
            raise HTTPException(409, f"Story is not in PENDING status (current: {story.status.value})")

        # Check if CuratedPost already exists for this story (idempotency)
        from src.schema.models import CuratedPost
        existing_post_stmt = select(CuratedPost).where(CuratedPost.story_id == story_id)
        existing_post_result = await session.execute(existing_post_stmt)
        existing_post = existing_post_result.scalar_one_or_none()
        if existing_post:
            raise HTTPException(409, f"Story already has a curated post (id: {existing_post.id})")

        # Get source URLs and key facts for caption generation
        stmt = (
            select(RawArticle)
            .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story_id)
        )
        result = await session.execute(stmt)
        articles = result.scalars().all()
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
        stories = await _render_stories_grid(session)

    return templates.TemplateResponse(request, "story_grid.html", {
        "request": request,
        "stories": stories,
        "notice": (
            APPROVE_AI_NOTICE if ai_assisted else APPROVE_NO_LLM_NOTICE
        ),
        "notice_type": "info" if ai_assisted else "warning",
    })


@app.post("/story/{story_id}/reject")
async def reject_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Reject a story - returns stories grid fragment for HTMX."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        story.status = Story.Status.REJECTED
        story.updated_at = datetime.now(timezone.utc)
        await session.commit()

        # Return updated stories grid fragment
        stories = await _render_stories_grid(session)

    return templates.TemplateResponse(request, "story_grid.html", {
        "request": request,
        "stories": stories,
    })


@app.get("/story/{story_id}/edit", response_class=HTMLResponse)
async def edit_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Show edit form for a story.

    The draft is prefilled from text already saved for the story. Otherwise it
    comes from the LLM when one is configured, and stays blank when none is, so
    the curator can write the caption by hand.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    llm_ok, _ = check_llm_available()

    from src.schema.models import MediaAsset, Snippet, SourceReliabilitySnapshot
    from sqlalchemy import select, desc

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        # Get source URLs
        stmt = (
            select(RawArticle)
            .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story_id)
        )
        result = await session.execute(stmt)
        articles = result.scalars().all()

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
    })


@app.post("/story/{story_id}/save")
async def save_story(
    story_id: uuid.UUID,
    request: Request,
    caption: str = Form(...),
    platform: str = Form("twitter"),
    override_validation: bool = Form(False),  # Allow manual override
    user: str = Depends(require_auth),
):
    """Save edited story as curated post.

    The caption always comes from the form, so this path never calls an LLM,
    with or without a configured provider.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        # Only allow saving of PENDING stories
        if story.status != Story.Status.PENDING:
            raise HTTPException(409, f"Story is not in PENDING status (current: {story.status.value})")

        # Get source articles for validation
        stmt = (
            select(RawArticle)
            .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story_id)
        )
        result = await session.execute(stmt)
        articles = result.scalars().all()
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


@app.get("/posts", response_class=HTMLResponse)
async def list_posts(request: Request, user: str = Depends(require_auth)):
    """Show approved posts ready for publishing."""
    db_ok, db_msg = check_database_available()
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
    })


@app.post("/post/{post_id}/mark-posted")
async def mark_posted(post_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Mark a post as published."""
    db_ok, db_msg = check_database_available()
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


@app.get("/api/stories/{story_id}/viewpoints")
async def get_story_viewpoints(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Get all viewpoint sub-clusters for a story.

    Stays behind auth even though it exposes no gate notes: the payload is the
    gate's own input (tier counts and distinct_owners per perspective, which is
    what apply_tier1_gate weighs) over stories of any status, so publishing it
    would hand anonymous readers the curation queue's decisions before curation
    has made them.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        # Get the main story
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        # Get viewpoint sub-clusters (stories with viewpoint_cluster_id = story.id)
        stmt = select(Story).where(Story.viewpoint_cluster_id == story_id)
        result = await session.execute(stmt)
        viewpoint_stories = result.scalars().all()

        # Also include the main story if it has no viewpoint_cluster_id
        # (it's the "master" story)
        all_viewpoints = []

        # Add main story as "overview" viewpoint
        main_units = story.units
        main_articles = [unit.representative for unit in main_units if unit.representative]
        all_viewpoints.append({
            "type": "overview",
            "label": "All Perspectives",
            "story_id": str(story.id),
            "unit_count": len(main_units),
            "articles": [
                {
                    "title": a.title,
                    "url": a.url,
                    "source_domain": a.source_domain,
                    "source_tier": a.source_tier.value,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                }
                for a in main_articles
            ],
            "tier1_count": story.tier1_unit_count,
            "tier2_count": story.tier2_unit_count,
            "tier3_count": story.tier3_unit_count,
            "tier4_count": story.tier4_unit_count,
            "distinct_owners": story.distinct_owners,
        })

        # Add each viewpoint sub-cluster
        for vs in viewpoint_stories:
            units = vs.units
            articles = [unit.representative for unit in units if unit.representative]
            all_viewpoints.append({
                "type": "viewpoint",
                "label": "Perspective",  # Could be enhanced with LLM-generated labels
                "story_id": str(vs.id),
                "unit_count": len(units),
                "articles": [
                    {
                        "title": a.title,
                        "url": a.url,
                        "source_domain": a.source_domain,
                        "source_tier": a.source_tier.value,
                        "published_at": a.published_at.isoformat() if a.published_at else None,
                    }
                    for a in articles
                ],
                "tier1_count": vs.tier1_unit_count,
                "tier2_count": vs.tier2_unit_count,
                "tier3_count": vs.tier3_unit_count,
                "tier4_count": vs.tier4_unit_count,
                "distinct_owners": vs.distinct_owners,
            })

    return {
        "story_id": str(story_id),
        "viewpoints": all_viewpoints,
        "total_viewpoints": len(all_viewpoints),
    }


@app.get("/api/stories/{story_id}/sources")
async def get_story_sources(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Get source breakdown for a story (tiers, owners, geographic).

    Stays behind auth: the per-tier and per-owner distributions are the
    clustering pipeline's internal view of how ownership is grouped for the
    gate, and the story is looked up by id regardless of status, so it also
    reads across stories a curator has not published yet.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        # Get all linked units with their source info
        stmt = (
            select(ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story_id)
        )
        result = await session.execute(stmt)
        units = result.scalars().all()

        tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0}
        owner_counts = {}
        geographic_counts = {}

        for unit in units:
            source_tiers = unit.source_tiers or {}
            owner_groups = unit.owner_groups or {}

            for tier, count in source_tiers.items():
                tier_counts[tier] = tier_counts.get(tier, 0) + count

            for owner, count in owner_groups.items():
                owner_counts[owner] = owner_counts.get(owner, 0) + count

            # Get geographic from representative article
            stmt = select(RawArticle).where(RawArticle.id == unit.representative_article_id)
            result = await session.execute(stmt)
            article = result.scalar_one_or_none()
            if article:
                # Could add geographic focus from source registry
                geo = article.source_domain  # placeholder
                geographic_counts[geo] = geographic_counts.get(geo, 0) + 1

    return {
        "story_id": str(story_id),
        "tier_distribution": tier_counts,
        "owner_distribution": owner_counts,
        "geographic_distribution": geographic_counts,
        "total_units": len(units),
    }


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

# Freshness stamp window and query guards.
MAP_FRESHNESS_WINDOW_HOURS = 24
MAP_FRESHNESS_OUTLET_STORY_CAP = 5000

# Top stories list.
MAP_TOP_STORIES_DEFAULT_LIMIT = 12
MAP_TOP_STORIES_MAX_LIMIT = 50


def _event_conditions(
    layer_id: uuid.UUID = None,
    event_type: str = None,
    min_confidence: float = None,
    bbox: str = None,
    start: datetime = None,
    end: datetime = None,
    min_tier1_sources: int = None,
) -> list:
    """Build the WHERE clauses shared by the event GeoJSON endpoints.

    An unparseable bbox is ignored (matches the historical globe behavior);
    start/end arrive already parsed by _parse_iso_timestamp.
    min_tier1_sources applies the tier 1 corroboration threshold.
    """
    conditions = []
    if layer_id:
        conditions.append(Event.layer_id == layer_id)
    if event_type:
        conditions.append(Event.event_type == event_type)
    if min_confidence:
        conditions.append(Event.confidence.cast(Float) >= min_confidence)
    if min_tier1_sources:
        conditions.append(Event.tier1_source_count >= min_tier1_sources)
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
            conditions.append(
                and_(
                    Event.longitude.cast(Float) >= min_lon,
                    Event.longitude.cast(Float) <= max_lon,
                    Event.latitude.cast(Float) >= min_lat,
                    Event.latitude.cast(Float) <= max_lat,
                )
            )
        except ValueError:
            pass  # Invalid bbox, ignore
    if start is not None:
        conditions.append(Event.start_time >= start)
    if end is not None:
        conditions.append(Event.start_time <= end)
    return conditions


def _event_feature(event: Event) -> dict:
    """Serialize one Event as a GeoJSON Feature.

    Shared by /api/globe/events and /api/map/replay so both endpoints emit
    identical properties. Falls back to the event's own lat/lon when no
    EventGeometry row is attached.
    """
    properties = {
        "story_id": str(event.story_id),
        "layer_id": str(event.layer_id) if event.layer_id else None,
        "event_type": event.event_type.value if event.event_type else None,
        "confidence": float(event.confidence) if event.confidence else 0.5,
        "source_count": event.source_count,
        "tier1_source_count": event.tier1_source_count,
        "location_name": event.location_name,
        "location_type": event.location_type,
        "radius_km": float(event.radius_km) if event.radius_km else None,
        "start_time": event.start_time.isoformat() if event.start_time else None,
        "entities": event.entities,
    }

    geometry = event.geometry
    if geometry and geometry.geojson:
        return {
            "type": "Feature",
            "id": str(event.id),
            "geometry": geometry.geojson,
            "properties": {**properties, **(geometry.properties or {})},
        }

    return {
        "type": "Feature",
        "id": str(event.id),
        "geometry": {
            "type": "Point",
            "coordinates": [float(event.longitude), float(event.latitude)],
        },
        "properties": properties,
    }


# Globe API endpoints
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


@app.get("/api/globe/events")
async def get_globe_events(
    request: Request,
    layer_id: uuid.UUID = None,
    event_type: str = None,
    min_confidence: float = None,
    bbox: str = None,  # min_lon,min_lat,max_lon,max_lat
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
    min_tier1_sources: int = None,
    limit: int = 500,
):
    """Get events as GeoJSON FeatureCollection for globe visualization.

    Public and read only. The default window is the last MAP_DEFAULT_WINDOW_HOURS
    hours filtered to corroborated events (tier1_source_count >= 2, the same
    threshold apply_tier1_gate uses for stories); pass hours=0 for all time and
    min_tier1_sources=0 to turn the corroboration filter off.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    window_start, window_end = _resolve_window(hours, datetime.now(timezone.utc))
    threshold = _corroboration_filter(min_tier1_sources)

    async with get_session() as session:
        stmt = select(Event).options(selectinload(Event.geometry), selectinload(Event.layer))

        conditions = _event_conditions(
            layer_id=layer_id,
            event_type=event_type,
            min_confidence=min_confidence,
            bbox=bbox,
            start=window_start,
            end=window_end,
            min_tier1_sources=threshold or None,
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(Event.start_time)).limit(limit)
        result = await session.execute(stmt)
        events = result.scalars().all()

    return {"type": "FeatureCollection", "features": [_event_feature(e) for e in events]}


@app.get("/api/globe/stats")
async def get_globe_stats(
    request: Request,
):
    """Get globe statistics."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    from sqlalchemy import select

    async with get_session() as session:
        # Total events
        total_result = await session.execute(select(func.count(Event.id)))
        total_events = total_result.scalar()

        # By event type
        type_result = await session.execute(
            select(Event.event_type, func.count(Event.id))
            .group_by(Event.event_type)
        )
        by_type = {row[0].value if row[0] else "other": row[1] for row in type_result.all()}

        # By layer
        layer_result = await session.execute(
            select(EventLayer.name, func.count(Event.id))
            .outerjoin(Event, Event.layer_id == EventLayer.id)
            .group_by(EventLayer.name)
        )
        by_layer = {row[0] or "unlayered": row[1] for row in layer_result.all()}

        # Date range
        date_result = await session.execute(
            select(func.min(Event.start_time), func.max(Event.start_time))
        )
        min_date, max_date = date_result.first()
        date_range = [
            min_date.isoformat() if min_date else None,
            max_date.isoformat() if max_date else None,
        ]

    return {
        "total_events": total_events,
        "by_type": by_type,
        "by_layer": by_layer,
        "date_range": date_range,
    }


@app.get("/api/globe/layers")
async def get_globe_layers(
    request: Request,
):
    """Get all event layers for globe visualization."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    from sqlalchemy import select

    async with get_session() as session:
        stmt = select(EventLayer).order_by(EventLayer.name)
        result = await session.execute(stmt)
        layers = result.scalars().all()

        return {
            "layers": [
                {
                    "id": str(layer.id),
                    "name": layer.name,
                    "description": layer.description,
                    "filter_criteria": layer.filter_criteria,
                    "style": layer.style,
                    "is_default": layer.is_default,
                    "is_visible": layer.is_visible,
                    "min_zoom": layer.min_zoom,
                    "max_zoom": layer.max_zoom,
                    "color": layer.color,
                }
                for layer in layers
            ]
        }


# Public map surfaces: top stories list, per story page and the freshness stamp.
#
# Everything below is read only and anonymous safe. The serializer deliberately
# omits the curation queue's internal fields (Story.status, Story.gate_reason,
# CuratedPost rows, claim evidence), so nothing here reports what a curator has
# decided or is about to decide.


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


@app.get("/api/map/freshness")
async def get_map_freshness(request: Request):
    """Freshness stamp for the public map. Read only, degrades to an empty stamp."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        return await _collect_map_freshness(session, now)


def _public_story_summary(story: Story, headline: str, event_count: int, latest: datetime) -> dict:
    """Public projection of one Story: what the pipeline corroborated, nothing else."""
    return {
        "story_id": str(story.id),
        "headline": headline or "Untitled story",
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
        "href": f"/stories/{story.id}",
    }


async def _collect_top_stories(
    session: AsyncSession,
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
    min_owners: int = MAP_CORROBORATION_MIN_OWNERS,
    limit: int = MAP_TOP_STORIES_DEFAULT_LIMIT,
    now: datetime = None,
) -> list:
    """Stories ranked by independent outlet count, using the existing gate fields.

    Ordering is Story.distinct_owners first, then Story.tier1_unit_count, then
    recency: the same corroboration the map's default view filters on. min_owners
    of 0 turns the gate filter off so full history stays reachable from the UI.
    """
    now = now or datetime.now(timezone.utc)
    window_start, _ = _resolve_window(hours, now)
    limit = max(1, min(int(limit or MAP_TOP_STORIES_DEFAULT_LIMIT), MAP_TOP_STORIES_MAX_LIMIT))

    from sqlalchemy.orm import selectinload

    event_stmt = (
        select(
            Event.story_id,
            func.count(Event.id).label("event_count"),
            func.max(Event.start_time).label("latest_at"),
        )
        .group_by(Event.story_id)
    )
    if window_start is not None:
        event_stmt = event_stmt.where(Event.start_time >= window_start)
    event_stmt = event_stmt.subquery()

    stmt = (
        select(Story, event_stmt.c.event_count, event_stmt.c.latest_at)
        .join(event_stmt, event_stmt.c.story_id == Story.id)
        .options(selectinload(Story.units).selectinload(ReportingUnit.representative))
        .order_by(
            desc(Story.distinct_owners),
            desc(Story.tier1_unit_count),
            desc(event_stmt.c.latest_at),
        )
        .limit(limit)
    )

    if min_owners and int(min_owners) > 0:
        stmt = stmt.where(
            Story.distinct_owners >= int(min_owners),
            Story.tier1_unit_count >= MAP_CORROBORATION_MIN_TIER1,
        )

    result = await session.execute(stmt)
    rows = result.all()

    summaries = []
    for story, event_count, latest_at in rows:
        headline = ""
        for unit in story.units:
            representative = getattr(unit, "representative", None)
            if representative is not None and representative.title:
                headline = representative.title
                break
        summaries.append(_public_story_summary(story, headline, event_count or 0, latest_at))
    return summaries


@app.get("/api/map/stories")
async def get_map_stories(
    request: Request,
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
    min_owners: int = MAP_CORROBORATION_MIN_OWNERS,
    limit: int = MAP_TOP_STORIES_DEFAULT_LIMIT,
):
    """Top stories for the public map list, ranked by corroboration. Read only."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stories = await _collect_top_stories(
            session,
            hours=hours,
            min_owners=min_owners,
            limit=limit,
        )

    return {
        "stories": stories,
        "count": len(stories),
        "window_hours": hours,
        "min_owners": min_owners,
    }


@app.get("/stories/{story_id}", response_class=HTMLResponse)
async def public_story_page(story_id: uuid.UUID, request: Request):
    """Public read only view of one story's corroboration.

    Shows the source articles and the corroboration counts, and nothing about
    the curation queue: no status, no gate reason, no curator decisions.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

        units_stmt = (
            select(ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story_id)
        )
        units_result = await session.execute(units_stmt)
        units = units_result.scalars().all()

        articles = []
        for unit in units:
            representative = getattr(unit, "representative", None)
            if representative is None:
                continue
            articles.append({
                "title": representative.title,
                "url": representative.url,
                "source_domain": representative.source_domain,
                "source_tier": (
                    representative.source_tier.value if representative.source_tier else None
                ),
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
        )
        events_result = await session.execute(events_stmt)
        events = events_result.scalars().all()

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
        "units": len(units),
        "day": story.day,
        "latest_at": latest_at,
        "corroborated": (
            int(story.tier1_unit_count or 0) >= MAP_CORROBORATION_MIN_TIER1
            and int(story.distinct_owners or 0) >= MAP_CORROBORATION_MIN_OWNERS
        ),
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


# Map replay API
#
# Replay sends the entire time window in a single response so the browser can
# scrub back and forth without refetching. That makes the result count the cost
# driver (payload size plus per-marker work in the client), so every response is
# capped at MAP_REPLAY_MAX_LIMIT events and the requested limit is clamped into
# [1, MAP_REPLAY_MAX_LIMIT]. Over-cap windows come back with "truncated": true
# and the oldest events dropped — narrow the window or filter by event_type
# rather than raising the cap.
MAP_REPLAY_DEFAULT_LIMIT = 500
MAP_REPLAY_MAX_LIMIT = 1000


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


@app.get("/api/map/replay")
async def get_map_replay(
    request: Request,
    start: str = None,  # ISO 8601, inclusive
    end: str = None,  # ISO 8601, inclusive
    event_type: str = None,
    min_tier1_sources: int = None,
    limit: int = MAP_REPLAY_DEFAULT_LIMIT,
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
    MAP_REPLAY_MAX_LIMIT; see the comment above that constant.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    start_dt = _parse_iso_timestamp(start, "start")
    end_dt = _parse_iso_timestamp(end, "end")
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start must be earlier than or equal to end.",
        )

    effective_limit = max(1, min(limit, MAP_REPLAY_MAX_LIMIT))
    threshold = _corroboration_filter(min_tier1_sources)

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    async with get_session() as session:
        stmt = select(Event).options(selectinload(Event.geometry), selectinload(Event.layer))

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

    truncated = len(events) > effective_limit
    events = events[:effective_limit]

    return {
        "type": "FeatureCollection",
        "features": [_event_feature(e) for e in events],
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat() if end_dt else None,
        "count": len(events),
        "limit": effective_limit,
        "max_limit": MAP_REPLAY_MAX_LIMIT,
        "truncated": truncated,
    }


# Globe page route
@app.get("/globe", response_class=HTMLResponse)
async def globe_page(request: Request):
    """Globe visualization page. Public and read only."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    return templates.TemplateResponse(request, "globe.html", {
        "request": request,
    })


# Flat map page route
@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    """Flat 2D map visualization page. Public and read only.

    The list of top stories and the freshness stamp are rendered server side so
    the page is useful before any JavaScript runs; map.js refreshes both from
    the public JSON endpoints.
    """
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        freshness = await _collect_map_freshness(session, now)
        top_stories = await _collect_top_stories(session, now=now)

    return templates.TemplateResponse(request, "map.html", {
        "request": request,
        "freshness": freshness,
        "top_stories": top_stories,
        "default_window_hours": MAP_DEFAULT_WINDOW_HOURS,
        "corroboration_min_tier1": MAP_CORROBORATION_MIN_TIER1,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)