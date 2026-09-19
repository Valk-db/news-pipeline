"""Build stories by grouping reporting units via top-N entity set overlap (Jaccard) on canonical IDs."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import ReportingUnit, Story, StoryUnitLink, RawArticle
from src.utils.ner import (
    get_primary_entity_set,
    entity_set_jaccard,
    EntityCanonicalizer,
    resolve_entities_to_canonical,
    canonical_jaccard,
)
from src.shared.config import get_settings
from datetime import datetime, timezone, timedelta
import uuid


async def build_stories(session: AsyncSession) -> list[uuid.UUID]:
    """
    Group reporting units into stories using top-N canonical entity Jaccard similarity.

    Matches new unlinked units against existing PENDING/BLOCKED stories from
    the last 48 hours. Attaches units to best-matching story if Jaccard ≥ 0.4,
    otherwise creates a new story.

    Returns list of story IDs that were created or modified.
    """
    settings = get_settings()
    top_n = settings.top_n_entities

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
    unit_ids = [u.id for u in new_units]
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

    return list(modified_story_ids)


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