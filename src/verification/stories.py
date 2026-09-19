"""Build stories by grouping reporting units via top-N entity set overlap (Jaccard)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import ReportingUnit, Story, StoryUnitLink, RawArticle
from src.utils.ner import get_primary_entity_set, entity_set_jaccard
from src.shared.config import get_settings
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import uuid


async def build_stories(session: AsyncSession) -> list[uuid.UUID]:
    """
    Group reporting units into stories using top-N entity set Jaccard similarity.

    Matches new unlinked units against existing PENDING/BLOCKED stories from
    the last 48 hours. Attaches units to best-matching story if Jaccard ≥ 0.4,
    otherwise creates a new story.

    Returns list of story IDs that were created or modified.
    """
    settings = get_settings()
    top_n = settings.top_n_entities

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

    # Build entity sets for each new unit
    unit_entities = {}
    for unit in new_units:
        article = articles.get(str(unit.representative_article_id))
        if article and article.entities:
            entity_set = get_primary_entity_set(article.entities)
            unit_entities[unit.id] = entity_set
        else:
            unit_entities[unit.id] = set()

    # Get recent stories (PENDING + BLOCKED) from last 48h with their aggregate entities
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent_stories = await _get_recent_stories_with_entities(session, cutoff)

    modified_story_ids = set()
    similarity_threshold = 0.4  # Jaccard threshold for attaching to existing story

    for unit in new_units:
        unit_entity_set = unit_entities.get(unit.id, set())
        if not unit_entity_set:
            # No entities - create new story
            story = await create_story_from_units(session, unit.day, [unit], unit_entities)
            modified_story_ids.add(story.id)
            continue

        # Find best matching story
        best_story_id = None
        best_jaccard = 0.0

        for story_id, story_entities in recent_stories:
            if not story_entities:
                continue
            jaccard = entity_set_jaccard(unit_entity_set, story_entities)
            if jaccard > best_jaccard and jaccard >= similarity_threshold:
                best_jaccard = jaccard
                best_story_id = story_id

        if best_story_id:
            # Attach to existing story
            await _attach_unit_to_story(session, unit.id, best_story_id)
            # Update story's aggregate entities
            await _update_story_entities(session, best_story_id, unit_entity_set)
            modified_story_ids.add(best_story_id)
        else:
            # Create new story
            story = await create_story_from_units(session, unit.day, [unit], unit_entities)
            modified_story_ids.add(story.id)

    return list(modified_story_ids)


async def _get_recent_stories_with_entities(
    session: AsyncSession,
    cutoff: datetime
) -> list[tuple[uuid.UUID, set[str]]]:
    """Get PENDING and BLOCKED stories from last 48h with their aggregate entity sets."""
    stmt = (
        select(Story)
        .where(Story.status.in_([Story.Status.PENDING, Story.Status.BLOCKED]))
        .where(Story.updated_at >= cutoff)
    )
    result = await session.execute(stmt)
    stories = result.scalars().all()

    story_entities = []
    for story in stories:
        # Get all units linked to this story
        stmt = (
            select(ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story.id)
        )
        result = await session.execute(stmt)
        units = result.scalars().all()

        # Aggregate entity sets
        combined_entities = set()
        for unit in units:
            combined_entities.update(unit.primary_entities or [])

        story_entities.append((story.id, combined_entities))

    return story_entities


async def _attach_unit_to_story(session: AsyncSession, unit_id: uuid.UUID, story_id: uuid.UUID) -> None:
    """Attach a unit to an existing story via StoryUnitLink."""
    link = StoryUnitLink(story_id=story_id, unit_id=unit_id)
    session.add(link)
    await session.flush()


async def _update_story_entities(session: AsyncSession, story_id: uuid.UUID, new_entities: set[str]) -> None:
    """Update story's primary_entities with new entities."""
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
    unit_entities: dict,
    combined_entities: set = None
) -> Story:
    """Create a Story and link units to it."""
    if combined_entities is None:
        combined_entities = set()
        for unit in units:
            combined_entities.update(unit_entities.get(unit.id, set()))

    story = Story(
        day=day,
        primary_entities=list(combined_entities)[:10],  # Store top 10 for display
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