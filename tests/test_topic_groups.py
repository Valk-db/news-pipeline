"""Tests for topic group seeding and assignment."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.verification.topics import _match_topic_groups, assign_story_topic_groups
from src.schema.models import Story, TopicGroup, StoryTopicGroup
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


@pytest.fixture
def sample_story():
    """Create a sample story with entities."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["Israel", "Hamas", "Gaza", "Government", "Diplomat"],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.QUEUED,
    )


@pytest.fixture
def sample_story_economy():
    """Create a sample story with economy entities."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["Stock Market", "Inflation", "Federal Reserve", "Interest Rate", "GDP"],
        tier1_unit_count=1,
        tier2_unit_count=2,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.QUEUED,
    )


@pytest.fixture
def sample_story_no_entities():
    """Create a sample story with no entities."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[],
        tier1_unit_count=0,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=0,
        status=Story.Status.QUEUED,
    )


def test_match_topic_groups_middle_east():
    """Test matching Middle East entities."""
    entities = ["Israel", "Hamas", "Gaza", "Palestine"]
    matches = _match_topic_groups(entities)

    assert len(matches) > 0
    # Should match Geopolitics > Middle East with high confidence
    middle_east_match = [m for m in matches if "Middle East" in m[0]]
    assert len(middle_east_match) == 1
    assert middle_east_match[0][1] > 50  # High confidence


def test_match_topic_groups_economy():
    """Test matching Economy entities."""
    entities = ["Stock Market", "Inflation", "Federal Reserve", "Interest Rate"]
    matches = _match_topic_groups(entities)

    assert len(matches) > 0
    economy_match = [m for m in matches if "Economy" in m[0] and ">" not in m[0]]
    assert len(economy_match) == 1
    assert economy_match[0][1] > 30


def test_match_topic_groups_multiple():
    """Test matching multiple topic groups."""
    entities = ["Israel", "Gaza", "Stock Market", "Inflation"]
    matches = _match_topic_groups(entities)

    assert len(matches) >= 2
    # Should be sorted by confidence descending
    assert matches[0][1] >= matches[1][1]


def test_match_topic_groups_no_match_fallback():
    """Test fallback when no entities match."""
    entities = ["UnknownEntity123", "AnotherUnknown456"]
    matches = _match_topic_groups(entities)

    assert len(matches) == 1
    assert matches[0][0] == "Geopolitics"
    assert matches[0][1] == 10  # Low confidence fallback


def test_match_topic_groups_empty_entities():
    """Test fallback with empty entities."""
    matches = _match_topic_groups([])

    assert len(matches) == 1
    assert matches[0][0] == "Geopolitics"
    assert matches[0][1] == 10


@pytest.mark.asyncio
async def test_assign_story_topic_groups_basic(mock_session, sample_story):
    """Test basic topic group assignment."""
    story_id = sample_story.id

    # Mock topic group queries
    mock_session.execute = AsyncMock()

    # Create mock topic groups that would match
    geopolitics_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics")
    middle_east_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics > Middle East", parent_group_id=geopolitics_group.id)

    # Side effect for execute calls:
    # 1. Story query
    # 2. TopicGroup query for "Geopolitics"
    # 3. StoryTopicGroup check for Geopolitics (returns None = new)
    # 4. TopicGroup query for "Geopolitics > Middle East"
    # 5. StoryTopicGroup check for Middle East (returns None = new)
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = sample_story

    # TopicGroup query results (in order of matches from _match_topic_groups)
    geopolitics_result = MagicMock()
    geopolitics_result.scalar_one_or_none.return_value = geopolitics_group

    middle_east_result = MagicMock()
    middle_east_result.scalar_one_or_none.return_value = middle_east_group

    # StoryTopicGroup check results (None = not assigned yet)
    existing_geopolitics = MagicMock()
    existing_geopolitics.scalar_one_or_none.return_value = None

    existing_middle_east = MagicMock()
    existing_middle_east.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [
        story_result,           # Story query
        geopolitics_result,     # TopicGroup "Geopolitics"
        existing_geopolitics,   # StoryTopicGroup check for Geopolitics
        middle_east_result,     # TopicGroup "Geopolitics > Middle East"
        existing_middle_east,   # StoryTopicGroup check for Middle East
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await assign_story_topic_groups(mock_session, story_id)

    assert len(result) >= 1
    assert all(r["created"] is True for r in result)
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_assign_story_topic_groups_already_assigned(mock_session):
    """Test that existing assignments are not duplicated."""
    story_id = uuid.uuid4()

    # Create a story with only Geopolitics entities to get a single match
    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=["Government", "Diplomat", "President"],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()

    geopolitics_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics")

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story

    group_result = MagicMock()
    group_result.scalar_one_or_none.return_value = geopolitics_group

    # Existing assignment
    existing_assignment = StoryTopicGroup(
        story_id=story_id,
        topic_group_id=geopolitics_group.id,
        confidence=50,
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing_assignment

    # 1. Story query
    # 2. TopicGroup query for "Geopolitics" (first match)
    # 3. StoryTopicGroup check (returns existing assignment)
    mock_session.execute.side_effect = [
        story_result,
        group_result,
        existing_result,
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await assign_story_topic_groups(mock_session, story_id)

    assert len(result) == 1
    assert result[0]["created"] is False
    assert result[0]["confidence"] == 50
    # Should not add or commit since already exists
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_assign_story_topic_groups_fallback(mock_session, sample_story_no_entities):
    """Test fallback assignment for story with no matching entities."""
    story_id = sample_story_no_entities.id

    mock_session.execute = AsyncMock()

    # Geopolitics group for fallback
    fallback_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics")

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = sample_story_no_entities

    group_result = MagicMock()
    group_result.scalar_one_or_none.return_value = fallback_group

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [
        story_result,
        group_result,  # No matches, so no specific group queries
        # Fallback query
    ]

    # The function will first try matches (none), then fallback
    # So we need to set up the fallback query
    mock_session.execute.side_effect = [
        story_result,  # Story query
        # No group queries since no matches
        # Fallback group query
        group_result,  # Fallback Geopolitics
        existing_result,  # Check existing
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await assign_story_topic_groups(mock_session, story_id)

    assert len(result) == 1
    assert result[0]["topic_group"] == "Geopolitics"
    assert result[0]["confidence"] == 10
    assert result[0]["created"] is True


@pytest.mark.asyncio
async def test_assign_story_topic_groups_not_found(mock_session):
    """Test assignment when story not found."""
    story_id = uuid.uuid4()

    mock_session.execute = AsyncMock()
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = story_result

    result = await assign_story_topic_groups(mock_session, story_id)

    assert len(result) == 1
    assert "error" in result[0]
    assert result[0]["error"] == "Story not found"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])