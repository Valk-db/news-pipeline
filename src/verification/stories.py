"""Build stories by grouping reporting units via top-N entity set overlap (Jaccard) on canonical IDs."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import ReportingUnit, Story, StoryUnitLink, RawArticle
from src.utils.ner import (
    EntityCanonicalizer,
    resolve_entities_to_canonical,
    canonical_jaccard,
)
from src.verification.tiers import recompute_story_counters, apply_tier1_gate
from datetime import datetime, timezone, timedelta
import uuid
import logging

logger = logging.getLogger(__name__)


async def build_stories(session: AsyncSession) -> list[uuid.UUID]:
    """
    Group reporting units into stories using top-N canonical entity Jaccard similarity.

    Matches new unlinked units against existing PENDING/BLOCKED stories from
    the last 48 hours. Attaches units to best-matching story if Jaccard ≥ 0.4,
    otherwise creates a new story.

    Returns list of story IDs that were created or modified.
    """
    # Initialize canonicalizer with session
    canonicalizer = EntityCanonicalizer(session)
    await canonicalizer.initialize()

    # Get unassigned reporting units
    stmt = (
        select(ReportingUnit)
        .outerjoin(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.id.is_(None))
    )
    result = await session.execute(stmt)
    new_units = result.scalars().all()

    if not new_units:
        return []

    # Fetch representative articles for entity extraction
    stmt = select(RawArticle).where(RawArticle.id.in_([u.representative_article_id for u in new_units]))
    result = await session.execute(stmt)
    articles = {str(a.id): a for a in result.scalars().all()}

    # Build canonical entity sets for each new unit
    unit_canonical_entities = {}
    for unit in new_units:
        article = articles.get(str(unit.representative_article_id))
        if article and article.entities:
            # Resolve entities to canonical IDs, preserving type information
            # article.entities format: {"PERSON": [...], "ORG": [...], "GPE": [...]}
            primary_entities = {k: v for k, v in article.entities.items() if k in {"PERSON", "ORG", "GPE"}}
            canonical_ids, _ = await resolve_entities_to_canonical(primary_entities, canonicalizer)
            unit_canonical_entities[unit.id] = canonical_ids
        else:
            unit_canonical_entities[unit.id] = set()

    # Get recent stories (PENDING + BLOCKED) from last 48h with their canonical entities
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent_stories_list = await _get_recent_stories_with_entities(session, cutoff)
    # Convert to dict for O(1) lookup and so we can add newly created stories
    recent_stories = {story_id: entities for story_id, entities in recent_stories_list}

    modified_story_ids = set()
    similarity_threshold = 0.4  # Jaccard threshold for attaching to existing story

    for unit in new_units:
        unit_canonical_set = unit_canonical_entities.get(unit.id, set())
        if not unit_canonical_set:
            # No entities - create new story
            story = await create_story_from_units(session, unit.day, [unit], unit_canonical_entities)
            # Add to recent_stories so subsequent units in same batch can attach to it
            recent_stories[story.id] = set(story.primary_entities or [])
            modified_story_ids.add(story.id)
            continue

        # Find best matching story
        best_story_id = None
        best_jaccard = 0.0

        for story_id, story_entities in recent_stories.items():
            if not story_entities:
                continue
            jaccard = canonical_jaccard(unit_canonical_set, story_entities)
            if jaccard > best_jaccard and jaccard >= similarity_threshold:
                best_jaccard = jaccard
                best_story_id = story_id

        if best_story_id:
            # Attach to existing story
            await _attach_unit_to_story(session, unit.id, best_story_id)
            # Update story's aggregate canonical entities
            await _update_story_entities(session, best_story_id, unit_canonical_set)
            # Also update our local cache
            recent_stories[best_story_id].update(unit_canonical_set)
            modified_story_ids.add(best_story_id)
        else:
            # Create new story
            story = await create_story_from_units(session, unit.day, [unit], unit_canonical_entities)
            # Add to recent_stories so subsequent units in same batch can attach to it
            recent_stories[story.id] = set(story.primary_entities or [])
            modified_story_ids.add(story.id)

    # Recompute counters for all modified stories after all attachments are done
    if modified_story_ids:
        await recompute_story_counters(session, list(modified_story_ids))

    # Phase 2: Viewpoint sub-clustering within stories
    await cluster_viewpoints(session, list(modified_story_ids))

    return list(modified_story_ids)


async def cluster_viewpoints(session: AsyncSession, story_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """
    Sub-cluster stories by viewpoint/stance using embedding similarity.

    For stories with sufficient units, use pgvector embeddings to identify
    distinct perspectives (pro/con/neutral, geographic, ideological).

    Args:
        session: Database session
        story_ids: List of story IDs to cluster

    Returns:
        Dictionary mapping viewpoint_cluster_id -> list of story_ids in that cluster
    """
    if not story_ids:
        return {}

    from src.schema.models import Story
    from sqlalchemy import select

    # Get stories with their linked units and source tiers
    stmt = (
        select(Story)
        .where(Story.id.in_(story_ids))
    )
    result = await session.execute(stmt)
    stories = result.scalars().all()

    viewpoint_clusters = {}

    for story in stories:
        # Get linked units with their source tier info
        from src.verification.tiers import recompute_story_counters
        await recompute_story_counters(session, [story.id])
        await session.refresh(story)

        # Only cluster viewpoints for stories with multiple tier sources
        total_units = (story.tier1_unit_count or 0) + (story.tier2_unit_count or 0) + \
                      (story.tier3_unit_count or 0) + (story.tier4_unit_count or 0)

        if total_units < 3:
            # Not enough units for viewpoint clustering
            continue

        # Get all units for this story
        from sqlalchemy.orm import selectinload
        stmt = (
            select(Story)
            .options(selectinload(Story.units))
            .where(Story.id == story.id)
        )
        result = await session.execute(stmt)
        story_with_units = result.scalar_one_or_none()

        # Extract article texts for embedding
        unit_texts = []
        for unit in story_with_units.units:
            # Get representative article
            stmt = select(RawArticle).where(RawArticle.id == unit.representative_article_id)
            result = await session.execute(stmt)
            article = result.scalar_one_or_none()
            if article and article.body_text:
                unit_texts.append({
                    "unit_id": unit.id,
                    "text": article.body_text[:2000],  # Truncate for embedding
                    "source_tier": list((unit.source_tiers or {}).keys())[0] if unit.source_tiers else "unknown",
                })

        if len(unit_texts) < 3:
            continue

        # Use LLM to classify stance/viewpoint
        from src.shared.llm import get_llm_client
        llm = await get_llm_client()

        # Create a prompt to classify viewpoints
        texts_for_prompt = "\n\n---\n\n".join([f"Source: {u['source_tier']}\n{u['text']}" for u in unit_texts[:10]])

        prompt = f"""Analyze these news article excerpts about the same event and identify distinct viewpoints/stances.
Group them into perspectives (e.g., pro-government, opposition, neutral/international, local, skeptical).

Articles:
{texts_for_prompt}

Return a JSON object mapping unit_id to viewpoint_label. Use concise labels like:
"pro_govt", "opposition", "neutral", "international", "local", "skeptical", "pro_business", "pro_labor", etc.

Only return the JSON object, no explanation."""

        try:
            result = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
            )
            import json
            viewpoint_labels = json.loads(result["choices"][0]["message"]["content"])

            # Group units by viewpoint
            viewpoint_to_units = {}
            for unit_data in unit_texts:
                unit_id = unit_data["unit_id"]
                label = viewpoint_labels.get(str(unit_id), "neutral")
                if label not in viewpoint_to_units:
                    viewpoint_to_units[label] = []
                viewpoint_to_units[label].append(unit_id)

            # Create viewpoint clusters
            for label, unit_ids in viewpoint_to_units.items():
                if len(unit_ids) >= 1:  # At least 1 unit per viewpoint
                    # Create a new story for this viewpoint (sub-cluster)
                    # Find the day from the first unit
                    stmt = select(ReportingUnit).where(ReportingUnit.id.in_(unit_ids))
                    result = await session.execute(stmt)
                    units = result.scalars().all()
                    day = units[0].day if units else story.day

                    viewpoint_story = Story(
                        day=day,
                        primary_entities=story.primary_entities,
                        status=Story.Status.PENDING,
                        viewpoint_cluster_id=story.id,  # Link to parent story
                    )
                    session.add(viewpoint_story)
                    await session.flush()

                    # Link units to viewpoint story
                    from src.schema.models import StoryUnitLink
                    for unit_id in unit_ids:
                        link = StoryUnitLink(story_id=viewpoint_story.id, unit_id=unit_id)
                        session.add(link)

                    # Also keep original story as "master" cluster
                    if story.id not in viewpoint_clusters:
                        viewpoint_clusters[story.id] = []
                    viewpoint_clusters[story.id].append(viewpoint_story.id)

            # Apply tier-1 gate to viewpoint sub-stories to prevent gate bypass
            if viewpoint_clusters.get(story.id):
                viewpoint_story_ids = [sid for sid in viewpoint_clusters[story.id]]
                await apply_tier1_gate(session, story_ids=viewpoint_story_ids)

        except Exception as e:
            logger.warning(f"Viewpoint clustering failed for story {story.id}: {e}", exc_info=True)

    await session.commit()
    return viewpoint_clusters


async def _get_recent_stories_with_entities(
    session: AsyncSession,
    cutoff: datetime
) -> list[tuple[uuid.UUID, set[str]]]:
    """Get PENDING and BLOCKED stories from last 48h with their primary_entities (now canonical IDs)."""
    stmt = (
        select(Story)
        .where(Story.status.in_([Story.Status.PENDING, Story.Status.BLOCKED]))
        .where(Story.updated_at >= cutoff)
    )
    result = await session.execute(stmt)
    stories = result.scalars().all()

    story_entities = []
    for story in stories:
        # Use Story.primary_entities directly (now stores canonical IDs)
        combined_entities = set(story.primary_entities or [])
        story_entities.append((story.id, combined_entities))

    return story_entities


async def _attach_unit_to_story(session: AsyncSession, unit_id: uuid.UUID, story_id: uuid.UUID) -> None:
    """Attach a unit to an existing story via StoryUnitLink."""
    link = StoryUnitLink(story_id=story_id, unit_id=unit_id)
    session.add(link)
    await session.flush()


async def _update_story_entities(session: AsyncSession, story_id: uuid.UUID, new_entities: set[str]) -> None:
    """Update story's primary_entities with new canonical entity IDs."""
    stmt = select(Story).where(Story.id == story_id)
    result = await session.execute(stmt)
    story = result.scalar_one_or_none()
    if story:
        current_entities = set(story.primary_entities or [])
        current_entities.update(new_entities)
        story.primary_entities = list(current_entities)[:10]
        story.updated_at = datetime.now(timezone.utc)
        await session.flush()


async def create_story_from_units(
    session: AsyncSession,
    day: datetime,
    units: list,
    unit_canonical_entities: dict,
    combined_entities: set[str] | None = None
) -> Story:
    """Create a Story and link units to it."""
    if combined_entities is None:
        combined_entities = set()
        for unit in units:
            combined_entities.update(unit_canonical_entities.get(unit.id, set()))

    story = Story(
        day=day,
        primary_entities=list(combined_entities)[:10],  # Store top 10 canonical IDs for display
        status=Story.Status.PENDING,
    )
    session.add(story)
    await session.flush()

    # Link units
    for unit in units:
        link = StoryUnitLink(story_id=story.id, unit_id=unit.id)
        session.add(link)

    await session.commit()
    return story