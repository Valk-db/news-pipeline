"""Tests for narrative arc linking functionality."""

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.verification.narrative import _entity_jaccard, link_narrative_arcs
from src.schema.models import Story, EntityEdge, EdgePredicate
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


def test_entity_jaccard_basic():
    """Test basic Jaccard similarity calculation."""
    set1 = {"a", "b", "c"}
    set2 = {"b", "c", "d"}
    # Intersection: {b, c} = 2, Union: {a, b, c, d} = 4, Jaccard = 0.5
    assert _entity_jaccard(set1, set2) == 0.5


def test_entity_jaccard_no_overlap():
    """Test Jaccard with no overlap."""
    set1 = {"a", "b"}
    set2 = {"c", "d"}
    assert _entity_jaccard(set1, set2) == 0.0


def test_entity_jaccard_identical():
    """Test Jaccard with identical sets."""
    set1 = {"a", "b", "c"}
    set2 = {"a", "b", "c"}
    assert _entity_jaccard(set1, set2) == 1.0


def test_entity_jaccard_empty():
    """Test Jaccard with empty set."""
    assert _entity_jaccard(set(), {"a", "b"}) == 0.0
    assert _entity_jaccard({"a", "b"}, set()) == 0.0
    assert _entity_jaccard(set(), set()) == 0.0


@pytest.mark.asyncio
async def test_link_narrative_arcs_same_event(mock_session):
    """Test SAME_EVENT_AS edge for high Jaccard overlap."""
    story_id = uuid.uuid4()
    older_story_id = uuid.uuid4()

    # Target story with entities
    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=["Israel", "Hamas", "Gaza", "Netanyahu", "Ceasefire"],
        status=Story.Status.QUEUED,
    )

    # Older story with high overlap (4/5 = 0.8 Jaccard)
    older_story = Story(
        id=older_story_id,
        day=datetime.now(timezone.utc) - timedelta(days=10),
        primary_entities=["Israel", "Hamas", "Gaza", "Netanyahu", "Hostages"],
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()

    # 1. Story query
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story

    # 2. Older stories query
    older_result = MagicMock()
    older_result.scalars.return_value.all.return_value = [older_story]

    # 3. Edge existence check (returns None = not exists)
    edge_result = MagicMock()
    edge_result.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [
        story_result,
        older_result,
        edge_result,
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await link_narrative_arcs(mock_session, story_id)

    assert len(result) == 1
    assert result[0]["predicate"] == "same_event_as"
    assert result[0]["confidence"] >= 50  # High confidence for 0.8 Jaccard
    assert result[0]["created"] is True
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_link_narrative_arcs_part_of_narrative(mock_session):
    """Test PART_OF_NARRATIVE edge for moderate Jaccard overlap."""
    story_id = uuid.uuid4()
    older_story_id = uuid.uuid4()

    # Target story
    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=["Israel", "Hamas", "Gaza", "Economy", "Inflation"],
        status=Story.Status.QUEUED,
    )

    # Older story with moderate overlap (2/7 = ~0.28 Jaccard)
    older_story = Story(
        id=older_story_id,
        day=datetime.now(timezone.utc) - timedelta(days=30),
        primary_entities=["Israel", "Economy", "Trade", "Markets"],
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story

    older_result = MagicMock()
    older_result.scalars.return_value.all.return_value = [older_story]

    edge_result = MagicMock()
    edge_result.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [
        story_result,
        older_result,
        edge_result,
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await link_narrative_arcs(mock_session, story_id)

    assert len(result) == 1
    assert result[0]["predicate"] == "part_of_narrative"
    assert result[0]["confidence"] >= 20  # Moderate confidence
    assert result[0]["created"] is True


@pytest.mark.asyncio
async def test_link_narrative_arcs_no_edge(mock_session):
    """Test no edge created for low overlap."""
    story_id = uuid.uuid4()
    older_story_id = uuid.uuid4()

    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=["Technology", "AI", "Startup", "Funding"],
        status=Story.Status.QUEUED,
    )

    # Completely different domain
    older_story = Story(
        id=older_story_id,
        day=datetime.now(timezone.utc) - timedelta(days=5),
        primary_entities=["Sports", "Football", "World Cup", "Goal"],
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story

    older_result = MagicMock()
    older_result.scalars.return_value.all.return_value = [older_story]

    mock_session.execute.side_effect = [
        story_result,
        older_result,
    ]

    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    result = await link_narrative_arcs(mock_session, story_id)

    assert len(result) == 0
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_link_narrative_arcs_story_not_found(mock_session):
    """Test error when story not found."""
    story_id = uuid.uuid4()

    mock_session.execute = AsyncMock()
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = story_result

    result = await link_narrative_arcs(mock_session, story_id)

    assert len(result) == 1
    assert "error" in result[0]
    assert result[0]["error"] == "Story not found"


@pytest.mark.asyncio
async def test_link_narrative_arcs_no_entities(mock_session):
    """Test no edges for story with no entities."""
    story_id = uuid.uuid4()

    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=[],
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    mock_session.execute.return_value = story_result

    result = await link_narrative_arcs(mock_session, story_id)

    assert len(result) == 0


@pytest.mark.asyncio
async def test_link_narrative_arcs_idempotent(mock_session):
    """Test that existing edges are not duplicated."""
    story_id = uuid.uuid4()
    older_story_id = uuid.uuid4()

    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=["Israel", "Hamas", "Gaza", "Netanyahu"],
        status=Story.Status.QUEUED,
    )

    older_story = Story(
        id=older_story_id,
        day=datetime.now(timezone.utc) - timedelta(days=10),
        primary_entities=["Israel", "Hamas", "Gaza", "Netanyahu"],
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story

    older_result = MagicMock()
    older_result.scalars.return_value.all.return_value = [older_story]

    # Existing edge
    existing_edge = EntityEdge(
        subject_type="story",
        subject_id=story_id,
        predicate=EdgePredicate.SAME_EVENT_AS,
        object_type="story",
        object_id=older_story_id,
        confidence=80,
    )
    edge_result = MagicMock()
    edge_result.scalar_one_or_none.return_value = existing_edge

    mock_session.execute.side_effect = [
        story_result,
        older_result,
        edge_result,
    ]

    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    result = await link_narrative_arcs(mock_session, story_id)

    assert len(result) == 1
    assert result[0]["created"] is False
    assert result[0]["confidence"] == 80
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_link_narrative_arcs_self_excluded(mock_session):
    """Test that story doesn't get edged to itself - the SQL query excludes it."""
    story_id = uuid.uuid4()

    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=["Israel", "Hamas", "Gaza"],
        status=Story.Status.QUEUED,
    )

    # The SQL query excludes the story by ID, so older_stories should not include it
    mock_session.execute = AsyncMock()

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story

    # Empty list because query excludes the story_id
    older_result = MagicMock()
    older_result.scalars.return_value.all.return_value = []

    mock_session.execute.side_effect = [
        story_result,
        older_result,
    ]

    mock_session.add = MagicMock()

    result = await link_narrative_arcs(mock_session, story_id)

    # Should not create self-edge (no older stories to link to)
    assert len(result) == 0
    mock_session.add.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])