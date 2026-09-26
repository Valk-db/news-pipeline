"""Tests for dynamic gate (P3-C: admission_score + harm_level)."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.verification.tiers import (
    compute_harm_level,
    compute_virality_signal,
    compute_admission_score,
    apply_dynamic_gate,
    evaluate_tier1_gate,
)
from src.shared.config import get_settings
from src.schema.models import (
    Story, ReportingUnit, Claim, ClaimType, CanonicalEntity
)
from sqlalchemy.ext.asyncio import AsyncSession


def make_scalar_mock(obj):
    """Create a mock result where scalar_one_or_none() returns obj (synchronous method)."""
    mock_result = MagicMock()
    # scalar_one_or_none is a SYNCHRONOUS method in SQLAlchemy Result
    mock_result.scalar_one_or_none = lambda: obj
    return mock_result


def make_result_mock(result_data):
    """Create a mock result object where scalars().all() returns result_data."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = result_data
    mock_result.all.return_value = result_data  # for queries using .all() directly
    return mock_result


def make_execute_mock(result_data):
    """Create an AsyncMock for session.execute that returns a result mock."""
    mock_result = make_result_mock(result_data)
    mock_execute = AsyncMock()
    mock_execute.return_value = mock_result
    return mock_execute


def make_execute_mock_for_side_effect(result_data):
    """Create a result mock for use directly in side_effect (not an AsyncMock wrapper)."""
    return make_result_mock(result_data)


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


@pytest.fixture
def sample_story():
    """Create a sample story."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[str(uuid.uuid4()), str(uuid.uuid4())],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.PENDING,
    )


@pytest.fixture
def sample_story_no_person():
    """Create a sample story with non-PERSON entities."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[str(uuid.uuid4()), str(uuid.uuid4())],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.PENDING,
    )


@pytest.fixture
def sample_story_high_harm():
    """Create a sample story with PERSON entities (for harm_level test)."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[str(uuid.uuid4()), str(uuid.uuid4())],  # UUID strings
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.PENDING,
    )


@pytest.fixture
def sample_story_viral():
    """Create a sample story with tier3/4 units for virality."""
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=[str(uuid.uuid4())],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=1,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.PENDING,
    )


def test_evaluate_tier1_gate_baseline():
    """Test the original boolean gate logic still works."""
    # Should pass: 2 tier-1 units, 2 distinct owners
    pairs = [("tier1", "BBC"), ("tier1", "Guardian")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is True

    # Should fail: only 1 tier-1 unit
    pairs = [("tier1", "BBC")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is False
    assert "Only 1 tier-1 units" in reason

    # Should fail: 2 tier-1 units but same owner
    pairs = [("tier1", "BBC"), ("tier1", "BBC")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is False
    assert "only 1 owner" in reason


@pytest.mark.asyncio
async def test_compute_harm_level_no_allegation(mock_session, sample_story):
    """Test harm_level returns 'low' when no ALLEGATION claim."""
    story = sample_story

    # First execute: Claim query → scalar_one_or_none() returns None
    claim_result = make_scalar_mock(None)
    mock_session.execute = AsyncMock(return_value=claim_result)

    result = await compute_harm_level(mock_session, story)
    assert result == "low"


@pytest.mark.asyncio
async def test_compute_harm_level_no_person_entity(mock_session, sample_story_no_person):
    """Test harm_level returns 'low' when ALLEGATION exists but no PERSON entity."""
    story = sample_story_no_person

    # First call: Claim query returns a claim (has ALLEGATION)
    claim = Claim(
        id=uuid.uuid4(),
        story_id=story.id,
        claim_type=ClaimType.ALLEGATION,
        text="Test allegation",
    )
    # Second call: CanonicalEntity query returns None (no PERSON)

    claim_result = make_scalar_mock(claim)
    entity_result = make_scalar_mock(None)
    mock_session.execute.side_effect = [claim_result, entity_result]

    result = await compute_harm_level(mock_session, story)
    assert result == "low"


@pytest.mark.asyncio
async def test_compute_harm_level_high(mock_session, sample_story_high_harm):
    """Test harm_level returns 'high' when ALLEGATION + PERSON entity."""
    story = sample_story_high_harm
    person_id = uuid.UUID(story.primary_entities[0])

    # First call: Claim query returns ALLEGATION claim
    claim = Claim(
        id=uuid.uuid4(),
        story_id=story.id,
        claim_type=ClaimType.ALLEGATION,
        text="Test allegation",
    )
    # Second call: CanonicalEntity query returns a PERSON
    person_entity = CanonicalEntity(
        id=person_id,
        canonical_name="John Doe",
        entity_type="PERSON",
    )

    claim_result = make_scalar_mock(claim)
    entity_result = make_scalar_mock(person_entity)
    mock_session.execute.side_effect = [claim_result, entity_result]

    result = await compute_harm_level(mock_session, story)
    assert result == "high"


@pytest.mark.asyncio
async def test_compute_virality_signal_no_viral(mock_session, sample_story):
    """Test virality_signal returns 0 when no tier3/4 units."""
    story = sample_story

    mock_session.execute = make_execute_mock([])
    virality = await compute_virality_signal(mock_session, story)
    assert virality == 0


@pytest.mark.asyncio
async def test_compute_virality_signal_with_viral(mock_session, sample_story_viral):
    """Test virality_signal returns max article_count for tier3/4 units."""
    story = sample_story_viral

    unit = ReportingUnit(
        id=uuid.uuid4(),
        article_count=5,
        source_tiers={"tier3": 1},
    )
    mock_session.execute = make_execute_mock([unit])

    virality = await compute_virality_signal(mock_session, story)
    assert virality == 5


@pytest.mark.asyncio
async def test_compute_admission_score_basic(mock_session, sample_story):
    """Test basic admission score calculation."""
    story = sample_story

    # compute_admission_score calls:
    # 1. compute_harm_level → 2 execute calls (claim + entity)
    # 2. compute_virality_signal → 1 execute call
    # Total: 3 execute calls

    # 1. harm_level: claim query → scalar_one_or_none returns None
    claim_result = make_scalar_mock(None)
    # 2. harm_level: entity query → scalar_one_or_none returns None (no PERSON)
    entity_result = make_scalar_mock(None)
    # 3. virality: execute returns empty list
    viral_result = make_execute_mock([])

    mock_session.execute.side_effect = [claim_result, entity_result, viral_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, 2)

    # tier_baseline=40 (tier1>=2) + corroboration=20 (2*10) + owners=10 (2*5) + virality=0 + harm=0 = 70
    assert breakdown["tier_baseline"] == 40
    assert breakdown["corroboration"] == 20
    assert breakdown["distinct_owners"] == 10
    assert breakdown["virality"] == 0
    assert breakdown["harm_penalty"] == 0
    assert breakdown["base_score"] == 70
    assert breakdown["final_score"] == 70
    assert breakdown["pass_threshold"] == 50
    assert breakdown["passes"] is True


@pytest.mark.asyncio
async def test_compute_admission_score_harm_high(mock_session, sample_story_high_harm):
    """Test admission score with harm_level='high'."""
    story = sample_story_high_harm

    # Harm level check: ALLEGATION claim exists (1 query)
    claim = Claim(
        id=uuid.uuid4(),
        story_id=story.id,
        claim_type=ClaimType.ALLEGATION,
        text="Test allegation",
    )
    claim_result = make_scalar_mock(claim)

    # PERSON entity exists (1 query)
    person_id = uuid.UUID(story.primary_entities[0])
    person_entity = CanonicalEntity(
        id=person_id,
        canonical_name="John Doe",
        entity_type="PERSON",
    )
    entity_result = make_scalar_mock(person_entity)

    # Virality check: no tier3/4 units (1 query)
    viral_result = make_execute_mock([])

    # Sequence: harm_level calls claim + entity, then virality calls viral
    mock_session.execute.side_effect = [claim_result, entity_result, viral_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, 2)

    # base_score=70, harm_penalty=-20, final=50
    assert breakdown["harm_level"] == "high"
    assert breakdown["harm_penalty"] == -20
    assert breakdown["base_score"] == 70
    assert breakdown["final_score"] == 50
    # pass_threshold is 70, score is 50 -> fails
    assert breakdown["pass_threshold"] == 70
    assert breakdown["passes"] is False


@pytest.mark.asyncio
async def test_compute_admission_score_virality(mock_session, sample_story_viral):
    """Test admission score with virality signal."""
    story = sample_story_viral

    # compute_admission_score calls:
    # 1. compute_virality_signal → 1 execute call (FIRST)
    # 2. compute_harm_level → 2 execute calls (claim + entity)
    # Total: 3 execute calls

    # 1. virality: execute returns tier3 unit
    unit = ReportingUnit(
        id=uuid.uuid4(),
        article_count=5,
        source_tiers={"tier3": 1},
    )
    viral_result = make_execute_mock_for_side_effect([unit])
    # 2. harm_level: claim query → scalar_one_or_none returns None
    claim_result = make_scalar_mock(None)
    # 3. harm_level: entity query → scalar_one_or_none returns None (no PERSON)
    entity_result = make_scalar_mock(None)

    # Set up the mock properly
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, 2)

    # base=70, virality=10 (5*2), final=80
    assert breakdown["virality"] == 10
    assert breakdown["final_score"] == 80
    assert breakdown["passes"] is True


@pytest.mark.asyncio
async def test_apply_dynamic_gate_shadow_mode(mock_session, sample_story):
    """Test that apply_dynamic_gate delegates to apply_tier1_gate when disabled."""
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = False

    try:
        # Call 1: Story query in apply_tier1_gate (select Story) -> scalars().all()
        story_result = make_execute_mock_for_side_effect([sample_story])

        # Call 2: recompute_story_counters first query (select StoryUnitLink, ReportingUnit) -> .all() returns tuples
        counter_query1_result = make_execute_mock_for_side_effect([
            (sample_story.id, {"tier1": 1}, {"BBC": 1}),
            (sample_story.id, {"tier1": 1}, {"Guardian": 1}),
        ])

        # Call 3: recompute_story_counters second query (select Story) -> scalars().all() returns Story objects
        counter_query2_result = make_execute_mock_for_side_effect([sample_story])

        # Call 4: Batched units query (select Story, ReportingUnit) -> .all() returns tuples
        unit = ReportingUnit(
            id=uuid.uuid4(),
            source_tiers={"tier1": 1},
            tier1_owner_groups={"BBC": 1},
        )
        batched_result = make_execute_mock_for_side_effect([(sample_story, unit)])

        mock_session.execute.side_effect = [
            story_result,
            counter_query1_result,
            counter_query2_result,
            batched_result,
        ]
        mock_session.commit = AsyncMock()

        result = await apply_dynamic_gate(mock_session, [sample_story.id])

        assert "queued" in result or "blocked" in result
    finally:
        settings.dynamic_gate_enabled = original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])