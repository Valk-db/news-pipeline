"""Build stories by grouping reporting units via top-N entity set overlap (Jaccard)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import ReportingUnit, Story, StoryUnitLink, RawArticle
from src.utils.ner import get_primary_entity_set, entity_set_jaccard
from src.shared.config import get_settings
from datetime import datetime, timezone
from collections import defaultdict
import uuid


async def build_stories(session: AsyncSession) -> int:
    """
    Group reporting units from the same day into stories using
    top-N entity set Jaccard similarity.
    """
    settings = get_settings()
    top_n = settings.top_n_entities

    # Get unassigned reporting units from last 24h
    # First, find units that aren't linked to any story yet
    stmt = (
        select(ReportingUnit)
        .outerjoin(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.id.is_(None))
    )
    result = await session.execute(stmt)
    units = result.scalars().all()

    if not units:
        return 0

    # Fetch representative articles for entity extraction
    unit_ids = [u.id for u in units]
    stmt = select(RawArticle).where(RawArticle.id.in_([u.representative_article_id for u in units]))
    result = await session.execute(stmt)
    articles = {str(a.id): a for a in result.scalars().all()}

    # Build entity sets for each unit
    unit_entities = {}
    for unit in units:
        article = articles.get(str(unit.representative_article_id))
        if article and article.entities:
            entity_set = get_primary_entity_set(article.entities)
            unit_entities[unit.id] = entity_set
        else:
            unit_entities[unit.id] = set()

    # Group by day first
    day_buckets = defaultdict(list)
    for unit in units:
        day_buckets[unit.day].append(unit)

    stories_created = 0
    similarity_threshold = 0.3  # Jaccard threshold for entity overlap

    for day, day_units in day_buckets.items():
        if len(day_units) == 1:
            # Single unit -> create story directly
            story = await create_story_from_units(session, day, [day_units[0]], unit_entities)
            stories_created += 1
            continue

        # Cluster by entity set overlap (union-find)
        parent = {u.id: u.id for u in day_units}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # Pairwise comparison within day bucket
        for i, u1 in enumerate(day_units):
            for u2 in day_units[i+1:]:
                set1 = unit_entities.get(u1.id, set())
                set2 = unit_entities.get(u2.id, set())
                if not set1 or not set2:
                    continue
                jaccard = entity_set_jaccard(set1, set2)
                if jaccard >= similarity_threshold:
                    union(u1.id, u2.id)

        # Collect clusters
        clusters = defaultdict(list)
        for unit in day_units:
            root = find(unit.id)
            clusters[root].append(unit)

        # Create story for each cluster
        for cluster_units in clusters.values():
            # Combine entity sets for story metadata
            combined_entities = set()
            for unit in cluster_units:
                combined_entities.update(unit_entities.get(unit.id, set()))

            await create_story_from_units(session, day, cluster_units, unit_entities, combined_entities)
            stories_created += 1

    return stories_created


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