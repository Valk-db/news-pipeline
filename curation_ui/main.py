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
from sqlalchemy import select, desc, and_, func
from sqlalchemy.types import Float
from sqlalchemy.ext.asyncio import AsyncSession
from src.shared.database import get_session
from src.schema.models import Story, StoryUnitLink, ReportingUnit, RawArticle, CuratedPost, Event, EventLayer
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


# Globe API endpoints
@app.get("/api/globe/events")
async def get_globe_events(
    request: Request,
    user: str = Depends(require_auth),
    layer_id: uuid.UUID = None,
    event_type: str = None,
    min_confidence: float = None,
    bbox: str = None,  # min_lon,min_lat,max_lon,max_lat
    limit: int = 500,
):
    """Get events as GeoJSON FeatureCollection for globe visualization."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return {"error": db_msg}

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    async with get_session() as session:
        stmt = select(Event).options(selectinload(Event.geometry), selectinload(Event.layer))

        # Apply filters
        conditions = []
        if layer_id:
            conditions.append(Event.layer_id == layer_id)
        if event_type:
            conditions.append(Event.event_type == event_type)
        if min_confidence:
            conditions.append(Event.confidence.cast(Float) >= min_confidence)
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

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(Event.start_time)).limit(limit)
        result = await session.execute(stmt)
        events = result.scalars().all()

        # Build GeoJSON FeatureCollection
        features = []
        for event in events:
            geometry = event.geometry
            if geometry and geometry.geojson:
                feature = {
                    "type": "Feature",
                    "id": str(event.id),
                    "geometry": geometry.geojson,
                    "properties": {
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
                        **(geometry.properties or {}),
                    },
                }
            else:
                # Fallback to point geometry
                feature = {
                    "type": "Feature",
                    "id": str(event.id),
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(event.longitude), float(event.latitude)],
                    },
                    "properties": {
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
                    },
                }
            features.append(feature)

    return {"type": "FeatureCollection", "features": features}


@app.get("/api/globe/stats")
async def get_globe_stats(
    request: Request,
    user: str = Depends(require_auth),
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
    user: str = Depends(require_auth),
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


# Globe page route
@app.get("/globe", response_class=HTMLResponse)
async def globe_page(request: Request, user: str = Depends(require_auth)):
    """Globe visualization page."""
    db_ok, db_msg = check_database_available()
    if not db_ok:
        return render_error_page(request, db_msg)

    return templates.TemplateResponse(request, "globe.html", {
        "request": request,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)