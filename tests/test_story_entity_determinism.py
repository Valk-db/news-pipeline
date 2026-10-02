"""Story.primary_entities must be a deterministic function of the entity set.

Set iteration order follows PYTHONHASHSEED, so truncating a set with [:10]
picked a different 10 on every process: stored entities churned between runs
and entities written by a previous run were evicted. The stored list is now
sorted, so the same input yields the same output in any process.
"""

import uuid
from datetime import datetime, timezone

import pytest

from src.schema.models import RawArticle, ReportingUnit
from src.verification.stories import (
    MAX_PRIMARY_ENTITIES,
    _update_story_entities,
    create_story_from_units,
)


def _canonical_ids(n: int, offset: int = 0) -> set[str]:
    return {str(uuid.UUID(int=offset + i)) for i in range(1, n + 1)}


async def _unit(db_session) -> ReportingUnit:
    now = datetime.now(timezone.utc)
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://example.test/{uuid.uuid4().hex[:8]}",
        url_hash=uuid.uuid4().hex,
        title="Test article",
        body_text="Body text",
        source_domain="example.test",
    )
    db_session.add(article)
    await db_session.flush()
    unit = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=article.id,
        day=now,
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"Example": 1},
        tier1_owner_groups={"Example": 1},
    )
    db_session.add(unit)
    await db_session.flush()
    return unit


@pytest.mark.asyncio
async def test_create_truncates_to_sorted_lowest_ids(db_session):
    """The kept 10 are the lexicographically lowest 10, in sorted order."""
    ids = _canonical_ids(25)
    unit = await _unit(db_session)
    story = await create_story_from_units(
        db_session, unit.day, [unit], {unit.id: ids}, combined_entities=ids
    )
    assert story.primary_entities == sorted(ids)[:MAX_PRIMARY_ENTITIES]


@pytest.mark.asyncio
async def test_merge_under_the_cap_keeps_everything_and_is_sorted(db_session):
    """Nothing already stored is evicted while the union still fits the cap."""
    stored = _canonical_ids(6)
    unit = await _unit(db_session)
    story = await create_story_from_units(
        db_session, unit.day, [unit], {unit.id: stored}, combined_entities=stored
    )

    incoming = _canonical_ids(4, offset=100)
    await _update_story_entities(db_session, story.id, incoming)
    await db_session.refresh(story)

    assert story.primary_entities == sorted(stored | incoming)


@pytest.mark.asyncio
async def test_merge_is_idempotent_across_repeated_passes(db_session):
    """Re-merging the same batch twice stores the same list both times.

    With an unsorted truncate the two passes stored different 10-element lists,
    so the story's identity silently changed under a no-op re-run.
    """
    stored = _canonical_ids(25)
    unit = await _unit(db_session)
    story = await create_story_from_units(
        db_session, unit.day, [unit], {unit.id: stored}, combined_entities=stored
    )
    incoming = _canonical_ids(25, offset=100)
    await _update_story_entities(db_session, story.id, incoming)
    await db_session.refresh(story)
    first_pass = list(story.primary_entities)

    await _update_story_entities(db_session, story.id, incoming)
    await db_session.refresh(story)

    assert story.primary_entities == first_pass == sorted(stored | incoming)[:MAX_PRIMARY_ENTITIES]


@pytest.mark.asyncio
async def test_short_entity_sets_are_stored_verbatim(db_session):
    """Fewer entities than the cap: all of them, still sorted."""
    ids = _canonical_ids(3)
    unit = await _unit(db_session)
    story = await create_story_from_units(
        db_session, unit.day, [unit], {unit.id: ids}, combined_entities=ids
    )
    assert story.primary_entities == sorted(ids)