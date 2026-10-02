"""The two authenticated per-story APIs the curation workbench calls.

Both read the clustering pipeline's internal view of a story (the tier counts and
owner groups the gate weighs, and the per-owner distributions that produced
them), so both stay behind require_auth even though neither exposes a gate note.
See the route docstrings for the reasoning; the public counterpart is
curation_ui.public_pages, which serves only approved stories.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from curation_ui.app_state import check_database
from curation_ui.security import require_auth
from src.schema.models import RawArticle, ReportingUnit, Story, StoryUnitLink
from src.shared.database import get_session

router = APIRouter()


@router.get("/api/stories/{story_id}/viewpoints")
async def get_story_viewpoints(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Get all viewpoint sub-clusters for a story.

    Stays behind auth even though it exposes no gate notes: the payload is the
    gate's own input (tier counts and distinct_owners per perspective, which is
    what apply_tier1_gate weighs) over stories of any status, so publishing it
    would hand anonymous readers the curation queue's decisions before curation
    has made them.
    """
    db_ok, db_msg = check_database(request)
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


@router.get("/api/stories/{story_id}/sources")
async def get_story_sources(story_id: uuid.UUID, request: Request, user: str = Depends(require_auth)):
    """Get source breakdown for a story (tiers, owners, geographic).

    Stays behind auth: the per-tier and per-owner distributions are the
    clustering pipeline's internal view of how ownership is grouped for the
    gate, and the story is looked up by id regardless of status, so it also
    reads across stories a curator has not published yet.
    """
    db_ok, db_msg = check_database(request)
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