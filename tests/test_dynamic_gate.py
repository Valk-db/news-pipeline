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
    """Create a mock result where scalar_one_or_none returns obj (sync method)."""
    mock_result = MagicMock()
    # scalar_one_or_none is a SYNCHRONOUS method in SQLAlchemy Result
    mock_result.scalar_one_or_none = MagicMock(return_value=obj)
    return mock_result


def make_result_mock(result_data):
    """Create a mock result object where scalars().all() returns result_data."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = result_data
    mock_result.all.return_value = result_data  # for queries using .all() directly
    return mock_result


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

    # Claim query returns None (no ALLEGATION)
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

    mock_session.execute.side_effect = [make_result_mock([])]
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
    mock_session.execute.side_effect = [make_result_mock([unit])]

    virality = await compute_virality_signal(mock_session, story)
    assert virality == 5


@pytest.mark.asyncio
async def test_compute_admission_score_basic(mock_session, sample_story):
    """Test basic admission score calculation."""
    story = sample_story

    # compute_admission_score calls:
    # 1. compute_virality_signal -> 1 execute call (FIRST)
    # 2. compute_harm_level -> 2 execute calls (claim + entity)
    # Total: 3 execute calls

    # 1. virality: execute returns empty list
    viral_result = make_result_mock([])
    # 2. harm_level: claim query -> scalar_one_or_none returns None
    claim_result = make_scalar_mock(None)
    # 3. harm_level: entity query -> scalar_one_or_none returns None (no PERSON)
    entity_result = make_scalar_mock(None)

    # Correct sequence: virality FIRST, then harm_level (claim, then entity)
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

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

    # compute_admission_score calls:
    # 1. compute_virality_signal -> 1 execute call (FIRST)
    # 2. compute_harm_level -> 2 execute calls (claim + entity)
    # Total: 3 execute calls

    # 1. virality: execute returns empty list (no tier3/4 units)
    viral_result = make_result_mock([])

    # 2. harm_level: claim query -> scalar_one_or_none returns ALLEGATION claim
    claim = Claim(
        id=uuid.uuid4(),
        story_id=story.id,
        claim_type=ClaimType.ALLEGATION,
        text="Test allegation",
    )
    claim_result = make_scalar_mock(claim)

    # 3. harm_level: entity query -> scalar_one_or_none returns PERSON entity
    person_id = uuid.UUID(story.primary_entities[0])
    person_entity = CanonicalEntity(
        id=person_id,
        canonical_name="John Doe",
        entity_type="PERSON",
    )
    entity_result = make_scalar_mock(person_entity)

    # Correct sequence: virality FIRST, then harm_level (claim, then entity)
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

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
    # 1. compute_virality_signal -> 1 execute call (FIRST)
    # 2. compute_harm_level -> 2 execute calls (claim + entity)
    # Total: 3 execute calls

    # 1. virality: execute returns tier3 unit
    unit = ReportingUnit(
        id=uuid.uuid4(),
        article_count=5,
        source_tiers={"tier3": 1},
    )
    viral_result = make_result_mock([unit])
    # 2. harm_level: claim query -> scalar_one_or_none returns None
    claim_result = make_scalar_mock(None)
    # 3. harm_level: entity query -> scalar_one_or_none returns None (no PERSON)
    entity_result = make_scalar_mock(None)

    # Correct sequence: virality FIRST, then harm_level (claim, then entity)
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, 2)

    # base=70, virality=10 (5*2), final=80
    assert breakdown["virality"] == 10
    assert breakdown["final_score"] == 80
    assert breakdown["passes"] is True


@pytest.mark.asyncio
async def test_apply_dynamic_gate_shadow_mode(mock_session, sample_story):
    """Test that apply_dynamic_gate delegates to apply_tier1_gate when disabled,
    and logs shadow-mode comparison to StatusLog."""
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = False

    try:
        # apply_dynamic_gate calls (shadow mode):
        # 1. select Story -> scalars().all()
        story_result = make_result_mock([sample_story])
        # 2. recompute_story_counters: select StoryUnitLink, ReportingUnit -> .all() tuples
        counter_query1_result = make_result_mock([
            (sample_story.id, {"tier1": 1}, {"BBC": 1}),
            (sample_story.id, {"tier1": 1}, {"Guardian": 1}),
        ])
        # 3. recompute_story_counters: select Story -> scalars().all() Story objects
        counter_query2_result = make_result_mock([sample_story])
        # 4. batch fetch units: select Story, ReportingUnit -> .all() tuples
        # Need 2 tier-1 units from different owners (BBC and Guardian)
        unit1 = ReportingUnit(
            id=uuid.uuid4(),
            source_tiers={"tier1": 1},
            tier1_owner_groups={"BBC": 1},
        )
        unit2 = ReportingUnit(
            id=uuid.uuid4(),
            source_tiers={"tier1": 1},
            tier1_owner_groups={"Guardian": 1},
        )
        batched_result = make_result_mock([(sample_story, unit1), (sample_story, unit2)])

        # apply_tier1_gate calls (inside shadow mode):
        # 5. select Story -> scalars().all()
        tier1_story_result = make_result_mock([sample_story])
        # 6. recompute_story_counters: select StoryUnitLink, ReportingUnit -> .all() tuples
        tier1_counter1_result = make_result_mock([
            (sample_story.id, {"tier1": 1}, {"BBC": 1}),
            (sample_story.id, {"tier1": 1}, {"Guardian": 1}),
        ])
        # 7. recompute_story_counters: select Story -> scalars().all() Story objects
        tier1_counter2_result = make_result_mock([sample_story])
        # 8. batch fetch units: select Story, ReportingUnit -> .all() tuples
        tier1_batched_result = make_result_mock([(sample_story, unit1), (sample_story, unit2)])

        # compute_admission_score calls (inside shadow mode, for logging):
        # 9. compute_virality_signal: select ReportingUnit -> scalars().all() []
        viral_result = make_result_mock([])
        # 10. compute_harm_level claim: scalar_one_or_none -> None
        claim_result = make_scalar_mock(None)
        # 11. compute_harm_level entity: scalar_one_or_none -> None
        entity_result = make_scalar_mock(None)

        mock_session.execute.side_effect = [
            story_result,           # 1
            counter_query1_result,  # 2
            counter_query2_result,  # 3
            batched_result,         # 4
            tier1_story_result,     # 5
            tier1_counter1_result,  # 6
            tier1_counter2_result,  # 7
            tier1_batched_result,   # 8
            viral_result,           # 9
            claim_result,           # 10
            entity_result,          # 11
        ]
        mock_session.commit = AsyncMock()

        result = await apply_dynamic_gate(mock_session, [sample_story.id])

        # Should return same queued/blocked as apply_tier1_gate
        assert "queued" in result
        assert "blocked" in result
        # Story should have passed (2 tier-1 units from BBC and Guardian)
        assert result["queued"] == 1
        assert result["blocked"] == 0

        # Should have logged to StatusLog (at least one add call for shadow comparison)
        assert mock_session.add.called
        # Check that a StatusLog was added with phase="gate_shadow"
        status_log_calls = [call for call in mock_session.add.call_args_list
                           if hasattr(call[0][0], '__tablename__') and call[0][0].__tablename__ == 'status_log']
        assert len(status_log_calls) >= 1
    finally:
        settings.dynamic_gate_enabled = original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])