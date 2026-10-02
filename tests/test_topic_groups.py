"""Tests for topic group seeding and assignment."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.verification.topics import (
    _match_topic_groups,
    _resolve_entity_names,
    assign_story_topic_groups,
)
from src.schema.models import Story, TopicGroup, StoryTopicGroup
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


# Canonical entity UUIDs mapped to the surface names the keyword matcher sees.
# primary_entities stores UUID strings (stories.create_story_from_units), so
# fixtures use UUID form and the mocked resolution query returns the names.
MIDDLE_EAST_EIDS = [uuid.uuid4() for _ in range(5)]
MIDDLE_EAST_NAMES = ["Israel", "Hamas", "Gaza", "Government", "Diplomat"]

ECONOMY_EIDS = [uuid.uuid4() for _ in range(5)]
ECONOMY_NAMES = ["Stock Market", "Inflation", "Federal Reserve", "Interest Rate", "GDP"]


def _names_result(names):
    """Mock for the _resolve_entity_names query: execute().scalars().all()."""
    scalars = MagicMock()
    scalars.all.return_value = list(names)
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


@pytest.fixture
def sample_story():
    """Create a sample story with UUID-form entities (Middle East)."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[str(eid) for eid in MIDDLE_EAST_EIDS],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.QUEUED,
    )


@pytest.fixture
def sample_story_economy():
    """Create a sample story with UUID-form entities (economy)."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[str(eid) for eid in ECONOMY_EIDS],
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
    """Test basic topic group assignment.

    Regression for P0-1: the story's primary_entities are canonical UUID
    strings; the old code keyword-matched the UUIDs directly, so every
    keyword scored 0 and the story always fell back to Geopolitics/10.
    This test fails on the old code (Middle East never queried) and passes
    once the UUIDs are resolved to canonical names first.
    """
    story_id = sample_story.id

    # Mock topic group queries
    mock_session.execute = AsyncMock()

    # Create mock topic groups that would match
    geopolitics_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics")
    middle_east_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics > Middle East", parent_group_id=geopolitics_group.id)

    # Side effect for execute calls (matches sorted by confidence desc:
    # "Geopolitics > Middle East" (55) before "Geopolitics" (40)):
    # 1. Story query
    # 2. Entity name resolution query (_resolve_entity_names)
    # 3. TopicGroup query for "Geopolitics > Middle East"
    # 4. StoryTopicGroup check for Middle East (returns None = new)
    # 5. TopicGroup query for "Geopolitics"
    # 6. StoryTopicGroup check for Geopolitics (returns None = new)
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
        _names_result(MIDDLE_EAST_NAMES),  # Entity name resolution
        middle_east_result,     # TopicGroup "Geopolitics > Middle East"
        existing_middle_east,   # StoryTopicGroup check for Middle East
        geopolitics_result,     # TopicGroup "Geopolitics"
        existing_geopolitics,   # StoryTopicGroup check for Geopolitics
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await assign_story_topic_groups(mock_session, story_id)

    assert len(result) == 2
    assert all(r["created"] is True for r in result)
    # UUIDs must have resolved to names: Middle East matched with confidence 55,
    # not the Geopolitics/10 fallback the old code produced for every story.
    middle_east = [r for r in result if r["topic_group"] == "Geopolitics > Middle East"]
    assert len(middle_east) == 1
    assert middle_east[0]["confidence"] == 55
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_assign_story_topic_groups_already_assigned(mock_session):
    """Test that existing assignments are not duplicated."""
    story_id = uuid.uuid4()

    # Create a story with only Geopolitics entities to get a single match
    gov_eids = [uuid.uuid4() for _ in range(3)]
    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=[str(e) for e in gov_eids],
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
    # 2. Entity name resolution query
    # 3. TopicGroup query for "Geopolitics" (first match)
    # 4. StoryTopicGroup check (returns existing assignment)
    mock_session.execute.side_effect = [
        story_result,
        _names_result(["Government", "Diplomat", "President"]),
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

    # The function will first try matches (none), then fallback.
    # Note: _resolve_entity_names makes no DB call for an empty list, so the
    # resolution query is not in this sequence.
    mock_session.execute.side_effect = [
        story_result,  # Story query
        group_result,  # Fallback Geopolitics group query
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


@pytest.mark.asyncio
async def test_resolve_entity_names(mock_session):
    """_resolve_entity_names: UUIDs resolve to canonical names, legacy
    surface strings pass through, unknown UUIDs are dropped."""
    known = uuid.uuid4()
    unknown = uuid.uuid4()
    entities = [str(known), "Plain Legacy Name", str(unknown)]

    mock_session.execute = AsyncMock()
    mock_session.execute.return_value = _names_result(["Resolved Name"])

    names = await _resolve_entity_names(mock_session, entities)

    assert "Resolved Name" in names
    assert "Plain Legacy Name" in names
    # The unknown UUID has no canonical row and no keyword text, so it is
    # dropped rather than fed to the matcher as a hex string.
    assert not any(str(unknown) in n for n in names)


@pytest.mark.asyncio
async def test_resolve_entity_names_empty(mock_session):
    """_resolve_entity_names with no entities makes no DB call."""
    mock_session.execute = AsyncMock()
    names = await _resolve_entity_names(mock_session, [])
    assert names == []
    mock_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_entity_names_non_list_payload(mock_session):
    """A non-list primary_entities payload (dict seen on dev rows) is
    treated as empty instead of being iterated."""
    mock_session.execute = AsyncMock()
    assert await _resolve_entity_names(mock_session, {"location": "Israel"}) == []
    assert await _resolve_entity_names(mock_session, '["not-a-uuid"]') == ["not-a-uuid"]
    mock_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_assign_story_topic_groups_uuid_no_keyword_match(mock_session):
    """UUID-form entities whose names match no keyword still fall back to
    Geopolitics/10 instead of erroring."""
    story_id = uuid.uuid4()
    story = Story(
        id=story_id,
        day=datetime.now(timezone.utc),
        primary_entities=[str(uuid.uuid4())],
        tier1_unit_count=1,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=1,
        status=Story.Status.QUEUED,
    )

    mock_session.execute = AsyncMock()
    fallback_group = TopicGroup(id=uuid.uuid4(), name="Geopolitics")

    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    group_result = MagicMock()
    group_result.scalar_one_or_none.return_value = fallback_group
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [
        story_result,
        _names_result(["Zzxq Unknownperson"]),  # resolves, matches no keyword
        group_result,
        existing_result,
    ]

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    result = await assign_story_topic_groups(mock_session, story_id)

    assert len(result) == 1
    assert result[0]["topic_group"] == "Geopolitics"
    assert result[0]["confidence"] == 10
    assert result[0]["created"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])