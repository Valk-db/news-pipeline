"""FastAPI + HTMX curation UI for story triage."""

import logging
import socket
import secrets

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
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from src.shared.database import get_session
from src.schema.models import (
    Story, StoryUnitLink, ReportingUnit, RawArticle, CuratedPost, SourceTier,
    User, CuratedStory, StoryAnnotation, Collection, CollectionStoryLink, Comment,
    User as UserTier,  # Alias to avoid conflict
)
from src.shared.llm import get_llm_client, validate_caption
from src.shared.config import get_settings
from curation_ui.health import router as health_router
from datetime import datetime, timezone
import uuid
import os

# Rate limiting for auth endpoints
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded


# Get the directory where this file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="News Pipeline Curation")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

settings = get_settings()

logger = logging.getLogger(__name__)

# Rate limiter for auth endpoints
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(health_router)

security = HTTPBasic()


@limiter.limit("10/minute")
async def require_auth(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> str:
    """Require HTTP Basic auth for all mutating endpoints."""
    if not settings.has_curation_auth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Curation UI not configured: CURATION_USER and CURATION_PASSWORD must be set",
        )
    ok_user = secrets.compare_digest(creds.username, settings.curation_user)
    ok_pass = secrets.compare_digest(creds.password, settings.curation_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def check_database_available() -> tuple[bool, str]:
    """Check if database is available, return (available, error_message)."""
    if not settings.has_database:
        return False, "Database not configured. Set DATABASE_URL environment variable."
    return True, ""


def check_llm_available() -> tuple[bool, str]:
    """Check if LLM is available, return (available, error_message)."""
    if not settings.has_llm:
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
    """Render the stories grid fragment for HTMX swap."""
    from sqlalchemy import select, desc
    from sqlalchemy.orm import selectinload
    from src.schema.models import Story, ReportingUnit

    stmt = (
        select(Story)
        .where(Story.status == Story.Status.PENDING)
        .order_by(desc(Story.day), desc(Story.created_at))
        .limit(50)
        .options(
            selectinload(Story.units).selectinload(ReportingUnit.representative)
        )
    )
    result = await session.execute(stmt)
    stories = result.scalars().all()

    story_data = []
    for story in stories:
        units = story.units
        articles = [unit.representative for unit in units if unit.representative]

        story_data.append({
            "story": story,
            "units": units,
            "articles": articles,
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
    """Approve a story for posting - generates caption via LLM and creates CuratedPost."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    llm_ok, llm_msg = check_llm_available()
    if not llm_ok:
        return render_error_page(request, llm_msg)

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

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

        # Generate caption via LLM
        llm = await get_llm_client()
        caption = await llm.generate_caption(
            story_title="; ".join(key_facts) if key_facts else "News Update",
            key_facts=key_facts,
            source_urls=source_urls,
            platform="twitter",
        )

        if not caption:
            raise HTTPException(500, "Failed to generate caption")

        # Create curated post
        post = CuratedPost(
            story_id=story_id,
            platform="twitter",
            caption=caption,
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
    """Show edit form for a story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    llm_ok, llm_msg = check_llm_available()
    if not llm_ok:
        return render_error_page(request, llm_msg)

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

        # Generate draft caption
        llm = await get_llm_client()
        caption = await llm.generate_caption(
            story_title="; ".join(key_facts) if key_facts else "News Update",
            key_facts=key_facts,
            source_urls=source_urls,
            platform="twitter",
        )

    return templates.TemplateResponse(request, "edit.html", {
        "request": request,
        "story": story,
        "articles": articles,
        "source_urls": source_urls,
        "key_facts": key_facts,
        "draft_caption": caption or "",
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
    """Save edited story as curated post."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    async with get_session() as session:
        stmt = select(Story).where(Story.id == story_id)
        result = await session.execute(stmt)
        story = result.scalar_one_or_none()
        if not story:
            raise HTTPException(404, "Story not found")

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
    """Get all viewpoint sub-clusters for a story."""
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
    """Get source breakdown for a story (tiers, owners, geographic)."""
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


# ============================================================
# CURATION API ENDPOINTS
# ============================================================

# --- User Management ---

@app.get("/api/user/profile")
async def get_user_profile(request: Request, user: str = Depends(require_auth)):
    """Get current user profile."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            # Create user if doesn't exist
            db_user = User(email=user, name=user.split("@")[0])
            session.add(db_user)
            await session.commit()
            await session.refresh(db_user)

        return {
            "id": str(db_user.id),
            "email": db_user.email,
            "name": db_user.name,
            "avatar_url": db_user.avatar_url,
            "tier": db_user.tier.value,
            "preferences": db_user.preferences,
        }


@app.patch("/api/user/profile")
async def update_user_profile(
    request: Request,
    name: str = Form(None),
    preferences: str = Form(None),
    user: str = Depends(require_auth),
):
    """Update user profile."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            raise HTTPException(404, "User not found")

        if name is not None:
            db_user.name = name
        if preferences is not None:
            import json
            try:
                db_user.preferences = json.loads(preferences)
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid preferences JSON")

        db_user.updated_at = datetime.now(timezone.utc)
        await session.commit()

    return {"success": True}


# --- Curated Stories ---

@app.get("/api/curated-stories")
async def list_curated_stories(
    request: Request,
    status: str = "draft",
    user: str = Depends(require_auth),
):
    """List user's curated stories."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            return {"stories": []}

        stmt = select(CuratedStory).where(CuratedStory.user_id == db_user.id)
        if status != "all":
            stmt = stmt.where(CuratedStory.status == CuratedStory.Status(status))
        stmt = stmt.order_by(desc(CuratedStory.updated_at))
        result = await session.execute(stmt)
        stories = result.scalars().all()

        return {
            "stories": [
                {
                    "id": str(s.id),
                    "title": s.title,
                    "description": s.description,
                    "story_ids": s.story_ids,
                    "narrative": s.narrative,
                    "is_public": s.is_public,
                    "tags": s.tags,
                    "status": s.status.value,
                    "version": s.version,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    "published_at": s.published_at.isoformat() if s.published_at else None,
                }
                for s in stories
            ]
        }


@app.post("/api/curated-stories")
async def create_curated_story(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    story_ids: str = Form("[]"),  # JSON array of story IDs
    narrative: str = Form(""),
    is_public: bool = Form(False),
    tags: str = Form("[]"),
    user: str = Depends(require_auth),
):
    """Create a new curated story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    import json
    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            raise HTTPException(404, "User not found")

        try:
            story_id_list = json.loads(story_ids)
            tag_list = json.loads(tags)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON for story_ids or tags")

        curated = CuratedStory(
            user_id=db_user.id,
            title=title,
            description=description,
            story_ids=story_id_list,
            narrative=narrative,
            is_public=is_public,
            tags=tag_list,
            status=CuratedStory.Status.DRAFT,
        )
        session.add(curated)
        await session.commit()
        await session.refresh(curated)

        return {"id": str(curated.id), "title": curated.title}


@app.get("/api/curated-stories/{story_id}")
async def get_curated_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Get a curated story with all details."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(CuratedStory).where(CuratedStory.id == story_id)
        result = await session.execute(stmt)
        curated = result.scalar_one_or_none()
        if not curated:
            raise HTTPException(404, "Curated story not found")

        # Check ownership
        if curated.user_id != db_user.id and not curated.is_public:
            raise HTTPException(403, "Not authorized")

        # Get source stories
        source_stories = []
        if curated.story_ids:
            stmt = select(Story).where(Story.id.in_(curated.story_ids))
            result = await session.execute(stmt)
            source_stories = result.scalars().all()

        # Get annotations
        stmt = select(StoryAnnotation).where(StoryAnnotation.curated_story_id == story_id)
        result = await session.execute(stmt)
        annotations = result.scalars().all()

        return {
            "id": str(curated.id),
            "title": curated.title,
            "description": curated.description,
            "story_ids": curated.story_ids,
            "source_stories": [
                {"id": str(s.id), "title": s.primary_entities, "day": s.day.isoformat() if s.day else None}
                for s in source_stories
            ],
            "narrative": curated.narrative,
            "is_public": curated.is_public,
            "tags": curated.tags,
            "status": curated.status.value,
            "version": curated.version,
            "annotations": [
                {
                    "id": str(a.id),
                    "type": a.annotation_type.value,
                    "content": a.content,
                    "position": a.position,
                    "selected_text": a.selected_text,
                    "color": a.color,
                    "is_private": a.is_private,
                }
                for a in annotations
            ],
            "created_at": curated.created_at.isoformat() if curated.created_at else None,
            "updated_at": curated.updated_at.isoformat() if curated.updated_at else None,
        }


@app.patch("/api/curated-stories/{story_id}")
async def update_curated_story(
    story_id: uuid.UUID,
    request: Request,
    title: str = Form(None),
    description: str = Form(None),
    story_ids: str = Form(None),
    narrative: str = Form(None),
    is_public: bool = Form(None),
    tags: str = Form(None),
    status: str = Form(None),
    user: str = Depends(require_auth),
):
    """Update a curated story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    import json
    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            raise HTTPException(404, "User not found")

        stmt = select(CuratedStory).where(CuratedStory.id == story_id)
        result = await session.execute(stmt)
        curated = result.scalar_one_or_none()
        if not curated:
            raise HTTPException(404, "Curated story not found")

        if curated.user_id != db_user.id:
            raise HTTPException(403, "Not authorized")

        if title is not None:
            curated.title = title
        if description is not None:
            curated.description = description
        if story_ids is not None:
            try:
                curated.story_ids = json.loads(story_ids)
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid story_ids JSON")
        if narrative is not None:
            curated.narrative = narrative
        if is_public is not None:
            curated.is_public = is_public
        if tags is not None:
            try:
                curated.tags = json.loads(tags)
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid tags JSON")
        if status is not None:
            curated.status = CuratedStory.Status(status)
            if status == "published" and not curated.published_at:
                curated.published_at = datetime.now(timezone.utc)

        curated.version += 1
        curated.updated_at = datetime.now(timezone.utc)
        await session.commit()

        return {"success": True, "version": curated.version}


@app.delete("/api/curated-stories/{story_id}")
async def delete_curated_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Delete a curated story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(CuratedStory).where(CuratedStory.id == story_id)
        result = await session.execute(stmt)
        curated = result.scalar_one_or_none()
        if not curated:
            raise HTTPException(404, "Curated story not found")

        if curated.user_id != db_user.id:
            raise HTTPException(403, "Not authorized")

        await session.delete(curated)
        await session.commit()

        return {"success": True}


@app.post("/api/curated-stories/{story_id}/fork")
async def fork_curated_story(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Fork a curated story (create a copy owned by current user)."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(CuratedStory).where(CuratedStory.id == story_id)
        result = await session.execute(stmt)
        original = result.scalar_one_or_none()
        if not original:
            raise HTTPException(404, "Curated story not found")

        if not original.is_public and original.user_id != db_user.id:
            raise HTTPException(403, "Not authorized to fork")

        forked = CuratedStory(
            user_id=db_user.id,
            title=f"{original.title} (Fork)",
            description=original.description,
            story_ids=original.story_ids,
            narrative=original.narrative,
            is_public=False,
            tags=original.tags,
            status=CuratedStory.Status.DRAFT,
            parent_id=original.id,
        )
        session.add(forked)
        await session.commit()
        await session.refresh(forked)

        return {"id": str(forked.id), "title": forked.title}


# --- Annotations ---

@app.post("/api/curated-stories/{story_id}/annotations")
async def create_annotation(
    story_id: uuid.UUID,
    request: Request,
    annotation_type: str = Form(...),
    content: str = Form(...),
    position: int = Form(None),
    selected_text: str = Form(""),
    color: str = Form("#ffff00"),
    is_private: bool = Form(True),
    article_id: str = Form(None),
    user: str = Depends(require_auth),
):
    """Add an annotation to a curated story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(CuratedStory).where(CuratedStory.id == story_id)
        result = await session.execute(stmt)
        curated = result.scalar_one_or_none()
        if not curated:
            raise HTTPException(404, "Curated story not found")

        if curated.user_id != db_user.id:
            raise HTTPException(403, "Not authorized")

        annotation = StoryAnnotation(
            user_id=db_user.id,
            curated_story_id=story_id,
            article_id=uuid.UUID(article_id) if article_id else None,
            annotation_type=StoryAnnotation.Type(annotation_type),
            content=content,
            position=position,
            selected_text=selected_text,
            color=color,
            is_private=is_private,
        )
        session.add(annotation)
        await session.commit()
        await session.refresh(annotation)

        return {"id": str(annotation.id), "content": annotation.content}


@app.delete("/api/annotations/{annotation_id}")
async def delete_annotation(annotation_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Delete an annotation."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(StoryAnnotation).where(StoryAnnotation.id == annotation_id)
        result = await session.execute(stmt)
        annotation = result.scalar_one_or_none()
        if not annotation:
            raise HTTPException(404, "Annotation not found")

        if annotation.user_id != db_user.id:
            raise HTTPException(403, "Not authorized")

        await session.delete(annotation)
        await session.commit()

        return {"success": True}


# --- Collections ---

@app.get("/api/collections")
async def list_collections(request: Request, user: str = Depends(require_auth)):
    """List user's collections."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            return {"collections": []}

        stmt = select(Collection).where(Collection.user_id == db_user.id)
        stmt = stmt.order_by(desc(Collection.updated_at))
        result = await session.execute(stmt)
        collections = result.scalars().all()

        return {
            "collections": [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "description": c.description,
                    "is_public": c.is_public,
                    "share_token": c.share_token,
                    "tags": c.tags,
                    "cover_image_url": c.cover_image_url,
                    "story_count": len(c.stories),
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                }
                for c in collections
            ]
        }


@app.post("/api/collections")
async def create_collection(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    is_public: bool = Form(False),
    tags: str = Form("[]"),
    user: str = Depends(require_auth),
):
    """Create a new collection."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    import json, secrets
    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()
        if not db_user:
            raise HTTPException(404, "User not found")

        try:
            tag_list = json.loads(tags)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid tags JSON")

        share_token = secrets.token_urlsafe(16) if is_public else None

        collection = Collection(
            user_id=db_user.id,
            name=name,
            description=description,
            is_public=is_public,
            share_token=share_token,
            tags=tag_list,
        )
        session.add(collection)
        await session.commit()
        await session.refresh(collection)

        return {"id": str(collection.id), "name": collection.name, "share_token": collection.share_token}


@app.post("/api/collections/{collection_id}/stories")
async def add_story_to_collection(
    collection_id: uuid.UUID,
    request: Request,
    curated_story_id: str = Form(...),
    user: str = Depends(require_auth),
):
    """Add a curated story to a collection."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(Collection).where(Collection.id == collection_id)
        result = await session.execute(stmt)
        collection = result.scalar_one_or_none()
        if not collection:
            raise HTTPException(404, "Collection not found")

        if collection.user_id != db_user.id:
            raise HTTPException(403, "Not authorized")

        # Get next position
        stmt = select(CollectionStoryLink).where(CollectionStoryLink.collection_id == collection_id)
        result = await session.execute(stmt)
        links = result.scalars().all()
        next_position = max([l.position for l in links], default=-1) + 1

        link = CollectionStoryLink(
            collection_id=collection_id,
            curated_story_id=uuid.UUID(curated_story_id),
            position=next_position,
        )
        session.add(link)
        await session.commit()

        return {"success": True}


@app.delete("/api/collections/{collection_id}/stories/{curated_story_id}")
async def remove_story_from_collection(
    collection_id: uuid.UUID,
    curated_story_id: uuid.UUID,
    request: Request,
    user: str = Depends(require_auth),
):
    """Remove a curated story from a collection."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(CollectionStoryLink).where(
            CollectionStoryLink.collection_id == collection_id,
            CollectionStoryLink.curated_story_id == curated_story_id,
        )
        result = await session.execute(stmt)
        link = result.scalar_one_or_none()
        if not link:
            raise HTTPException(404, "Link not found")

        # Verify ownership
        stmt = select(Collection).where(Collection.id == collection_id)
        result = await session.execute(stmt)
        collection = result.scalar_one_or_none()
        if collection.user_id != db_user.id:
            raise HTTPException(403, "Not authorized")

        await session.delete(link)
        await session.commit()

        return {"success": True}


# --- Export Endpoints ---

@app.get("/api/curated-stories/{story_id}/export")
async def export_curated_story(
    story_id: uuid.UUID,
    format: str = "json",
    request: Request = None,
    user: str = Depends(require_auth),
):
    """Export a curated story in various formats."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        stmt = select(CuratedStory).where(CuratedStory.id == story_id)
        result = await session.execute(stmt)
        curated = result.scalar_one_or_none()
        if not curated:
            raise HTTPException(404, "Curated story not found")

        if curated.user_id != db_user.id and not curated.is_public:
            raise HTTPException(403, "Not authorized")

        # Get source stories
        source_stories = []
        if curated.story_ids:
            stmt = select(Story).where(Story.id.in_(curated.story_ids))
            result = await session.execute(stmt)
            source_stories = result.scalars().all()

        # Get annotations
        stmt = select(StoryAnnotation).where(StoryAnnotation.curated_story_id == story_id)
        result = await session.execute(stmt)
        annotations = result.scalars().all()

        export_data = {
            "title": curated.title,
            "description": curated.description,
            "narrative": curated.narrative,
            "tags": curated.tags,
            "source_stories": [
                {
                    "id": str(s.id),
                    "primary_entities": s.primary_entities,
                    "day": s.day.isoformat() if s.day else None,
                    "tier1_units": s.tier1_unit_count,
                    "tier2_units": s.tier2_unit_count,
                    "distinct_owners": s.distinct_owners,
                }
                for s in source_stories
            ],
            "annotations": [
                {
                    "type": a.annotation_type.value,
                    "content": a.content,
                    "selected_text": a.selected_text,
                    "color": a.color,
                }
                for a in annotations
            ],
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": curated.version,
        }

        if format == "json":
            return export_data
        elif format == "markdown":
            md = f"# {curated.title}\n\n"
            if curated.description:
                md += f"{curated.description}\n\n"
            if curated.narrative:
                md += f"## Narrative\n\n{curated.narrative}\n\n"
            md += "## Source Stories\n\n"
            for s in source_stories:
                md += f"- Story {s.id[:8]}: {', '.join(s.primary_entities[:3])} ({s.day.strftime('%Y-%m-%d') if s.day else 'Unknown date'})\n"
            if annotations:
                md += "\n## Annotations\n\n"
                for a in annotations:
                    md += f"> **{a.annotation_type.value}**: {a.content}\n\n"
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(md, media_type="text/markdown")
        else:
            raise HTTPException(400, "Unsupported format")


# --- Comments ---

@app.get("/api/curated-stories/{story_id}/comments")
async def get_comments(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Get comments for a curated story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(Comment).where(Comment.curated_story_id == story_id, Comment.parent_id.is_(None))
        stmt = stmt.order_by(Comment.created_at)
        result = await session.execute(stmt)
        comments = result.scalars().all()

        def comment_to_dict(c):
            return {
                "id": str(c.id),
                "user_id": str(c.user_id),
                "content": c.content,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "replies": [comment_to_dict(r) for r in c.replies],
            }

        return {"comments": [comment_to_dict(c) for c in comments]}


@app.post("/api/curated-stories/{story_id}/comments")
async def add_comment(
    story_id: uuid.UUID,
    request: Request,
    content: str = Form(...),
    parent_id: str = Form(None),
    user: str = Depends(require_auth),
):
    """Add a comment to a curated story."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(User).where(User.email == user)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        comment = Comment(
            user_id=db_user.id,
            curated_story_id=story_id,
            parent_id=uuid.UUID(parent_id) if parent_id else None,
            content=content,
        )
        session.add(comment)
        await session.commit()
        await session.refresh(comment)

        return {"id": str(comment.id), "content": comment.content}


# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)