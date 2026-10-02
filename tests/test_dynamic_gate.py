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
from src.shared.analyzer_versions import GATE_VERSION
from src.shared.config import get_settings
from src.schema.models import (
    Story, ReportingUnit, Claim, ClaimType, CanonicalEntity
)
from sqlalchemy.ext.asyncio import AsyncSession


def make_first_mock(obj):
    """Create a mock result where scalars().first() returns obj (sync method).

    compute_harm_level takes the first matching row on purpose: a story
    routinely has several ALLEGATION claims and several PERSON entities, and
    the old scalar_one_or_none() raised MultipleResultsFound on exactly those
    (P0-2). This helper simulates any number of rows by returning the first.
    """
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = obj
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
    """Test the original boolean gate logic still works.

    The gate consumes one (unit_id, owner) pair per distinct tier-1 owner of each unit; the
    unit half of the rule counts distinct unit ids.
    """
    # Should pass: 2 tier-1 units, 2 distinct owners
    pairs = [(uuid.uuid4(), "BBC"), (uuid.uuid4(), "Guardian")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is True

    # Should fail: only 1 tier-1 unit
    pairs = [(uuid.uuid4(), "BBC")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is False
    assert "Only 1 tier-1 unit" in reason

    # Should fail: 2 tier-1 units but same owner
    pairs = [(uuid.uuid4(), "BBC"), (uuid.uuid4(), "BBC")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is False
    assert "only 1 owner" in reason


@pytest.mark.asyncio
async def test_compute_harm_level_no_allegation(mock_session, sample_story):
    """Test harm_level returns 'low' when no ALLEGATION claim."""
    story = sample_story

    # Claim query returns None (no ALLEGATION)
    claim_result = make_first_mock(None)
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

    claim_result = make_first_mock(claim)
    entity_result = make_first_mock(None)
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

    claim_result = make_first_mock(claim)
    entity_result = make_first_mock(person_entity)
    mock_session.execute.side_effect = [claim_result, entity_result]

    result = await compute_harm_level(mock_session, story)
    assert result == "high"


@pytest.mark.asyncio
async def test_compute_harm_level_multiple_allegations(mock_session, sample_story_high_harm):
    """P0-2: 2+ ALLEGATION claims must not raise; the first row is enough."""
    story = sample_story_high_harm
    person_id = uuid.UUID(story.primary_entities[0])

    claims = [
        Claim(
            id=uuid.uuid4(),
            story_id=story.id,
            claim_type=ClaimType.ALLEGATION,
            text="Test allegation one",
        ),
        Claim(
            id=uuid.uuid4(),
            story_id=story.id,
            claim_type=ClaimType.ALLEGATION,
            text="Test allegation two",
        ),
    ]
    person_entity = CanonicalEntity(
        id=person_id,
        canonical_name="John Doe",
        entity_type="PERSON",
    )

    claim_result = make_first_mock(claims[0])  # first of the two matching rows
    entity_result = make_first_mock(person_entity)
    mock_session.execute.side_effect = [claim_result, entity_result]

    assert await compute_harm_level(mock_session, story) == "high"


@pytest.mark.asyncio
async def test_compute_harm_level_multiple_persons(mock_session, sample_story_high_harm):
    """P0-2: 2+ PERSON entities (Trump-and-Biden story) must not raise."""
    story = sample_story_high_harm

    claim = Claim(
        id=uuid.uuid4(),
        story_id=story.id,
        claim_type=ClaimType.ALLEGATION,
        text="Test allegation",
    )
    persons = [
        CanonicalEntity(id=uuid.UUID(eid), canonical_name=f"Person {i}", entity_type="PERSON")
        for i, eid in enumerate(story.primary_entities)
    ]

    claim_result = make_first_mock(claim)
    entity_result = make_first_mock(persons[0])  # first of the two matching rows
    mock_session.execute.side_effect = [claim_result, entity_result]

    assert await compute_harm_level(mock_session, story) == "high"


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
    claim_result = make_first_mock(None)
    # 3. harm_level: entity query -> scalar_one_or_none returns None (no PERSON)
    entity_result = make_first_mock(None)

    # Correct sequence: virality FIRST, then harm_level (claim, then entity)
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, tier1_units=2)

    # 2 independent owners: baseline=40 (owners>=2) + corroboration=20 (2*10) + owners=10 (2*5)
    # + virality=0 + harm=0 = 70
    assert breakdown["independent_owners"] == 2
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
    claim_result = make_first_mock(claim)

    # 3. harm_level: entity query -> scalar_one_or_none returns PERSON entity
    person_id = uuid.UUID(story.primary_entities[0])
    person_entity = CanonicalEntity(
        id=person_id,
        canonical_name="John Doe",
        entity_type="PERSON",
    )
    entity_result = make_first_mock(person_entity)

    # Correct sequence: virality FIRST, then harm_level (claim, then entity)
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, tier1_units=2)

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
    claim_result = make_first_mock(None)
    # 3. harm_level: entity query -> scalar_one_or_none returns None (no PERSON)
    entity_result = make_first_mock(None)

    # Correct sequence: virality FIRST, then harm_level (claim, then entity)
    mock_session.execute.side_effect = [viral_result, claim_result, entity_result]

    score, breakdown = await compute_admission_score(mock_session, story, 2, tier1_units=2)

    # base=70, virality=10 (5*2), final=80
    assert breakdown["virality"] == 10
    assert breakdown["final_score"] == 80
    assert breakdown["passes"] is True


@pytest.mark.asyncio
async def test_score_counts_independent_owners_not_articles(mock_session, sample_story):
    """Four tier-1 articles from one owner score below the threshold; four owners do not.

    The collapse fixture's shape -- one outlet's reporting carried by several papers -- reached the
    old score as a high article count and passed, while the boolean gate blocked it. With the score
    counting independent owners, the two agree on independence: a single owner cannot reach 50, and
    the breakdown says which count it was given.

    V-P1-14: the score mirrors the boolean gate's FULL admission condition (>= 2 tier-1 units AND
    >= 2 distinct owners), not just the owners half. The independence factors award nothing when
    the condition fails -- a single reporting unit cannot corroborate itself -- so a story the
    boolean gate blocks on independence can score at most the virality 10 and can never reach 50.
    """
    for distinct_owners, expected_base in ((1, 0), (2, 70), (4, 90)):
        mock_session.execute.side_effect = [
            make_result_mock([]),  # compute_virality_signal: no tier3/4 units
            make_first_mock(None),  # compute_harm_level: no ALLEGATION claim
        ]
        score, breakdown = await compute_admission_score(
            mock_session, sample_story, distinct_owners, tier1_units=2
        )
        assert breakdown["independent_owners"] == distinct_owners
        assert breakdown["tier1_units"] == 2
        assert breakdown["base_score"] == expected_base
        assert score == expected_base
        assert breakdown["passes"] is (expected_base >= 50)


@pytest.mark.asyncio
async def test_score_baseline_requires_both_halves_of_the_gate(mock_session, sample_story):
    """V-P1-14: mirroring only the owners half still diverges where P0-4 bites.

    One reporting unit carrying four tier-1 articles from four distinct owners: the boolean gate
    blocks it ("Only 1 tier-1 unit"), so the score must not pass it either. A baseline that only
    checked owners would award 40 + 30 + 20 = 90 here; with both conditions mirrored the
    independence factors award nothing and the story scores 0.
    """
    mock_session.execute.side_effect = [
        make_result_mock([]),            # compute_virality_signal: no tier3/4 units
        make_first_mock(None),           # compute_harm_level: no ALLEGATION claim
    ]
    score, breakdown = await compute_admission_score(
        mock_session, sample_story, 4, tier1_units=1
    )
    assert breakdown["independent_owners"] == 4
    assert breakdown["tier1_units"] == 1
    assert breakdown["tier_baseline"] == 0
    assert breakdown["corroboration"] == 0
    assert breakdown["distinct_owners"] == 0
    assert breakdown["base_score"] == 0
    assert score == 0
    assert breakdown["passes"] is False


@pytest.mark.asyncio
async def test_apply_dynamic_gate_shadow_mode(mock_session, sample_story):
    """Shadow mode decides with the boolean gate but still records the score it computed.

    Two rows are appended per story in this mode -- the applied 'tier1' decision and the
    'dynamic' score marked breakdown["shadow"] = True -- so the calibration comparison is in
    the audit trail and not only in status_log.
    """
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = False

    try:
        # 2 tier-1 units from different owners, so the boolean gate passes.
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
        links = [
            (sample_story.id, unit1.id, None, {"tier1": 1}, {"BBC": 1}),
            (sample_story.id, unit2.id, None, {"tier1": 1}, {"Guardian": 1}),
        ]
        # The articles behind those units. No content_hash, so resolve_wire_origins short-circuits
        # on an empty hash set and issues no second query -- nothing here is a wire copy.
        articles = [
            (uuid.uuid4(), unit1.id, "https://bbc.com/a", "bbc.com", "tier1", None),
            (uuid.uuid4(), unit2.id, "https://theguardian.com/b", "theguardian.com", "tier1", None),
        ]

        def seq(*results):
            return [make_result_mock(r) for r in results]

        story_rows = [sample_story]
        batched_rows = [(sample_story, unit1), (sample_story, unit2)]

        # apply_dynamic_gate (shadow): stories, recompute (links, articles, stories, units),
        # then its own resolution before delegating.
        # apply_tier1_gate: stories, the same four, with the resolution handed in.
        # compute_admission_score: virality, claim, entity.
        mock_session.execute.side_effect = [
            *seq(story_rows),  # 1 apply_dynamic_gate: select Story
            *seq(links),  # 2 recompute: links
            *seq(articles),  # 3 recompute: load_corroboration articles
            *seq(story_rows),  # 4 recompute: select Story
            *seq(batched_rows),  # 5 apply_dynamic_gate: batch fetch units
            *seq(articles),  # 6 apply_dynamic_gate: load_corroboration articles
            *seq(story_rows),  # 7 apply_tier1_gate: select Story
            *seq(links),  # 8 recompute: links
            *seq(articles),  # 9 recompute: load_corroboration articles
            *seq(story_rows),  # 10 recompute: select Story
            *seq(batched_rows),  # 11 apply_tier1_gate: batch fetch units
            *seq([]),  # 12 compute_virality_signal
            make_first_mock(None),  # 13 compute_harm_level: no ALLEGATION claim
        ]
        mock_session.commit = AsyncMock()

        result = await apply_dynamic_gate(mock_session, [sample_story.id])

        assert result["queued"] == 1
        assert result["blocked"] == 0

        added = [call[0][0] for call in mock_session.add.call_args_list]
        status_logs = [o for o in added if o.__tablename__ == "status_log"]
        assert [log.phase for log in status_logs] == ["gate_shadow"]
        assert status_logs[0].details["agreement"] is True

        decisions = [o for o in added if o.__tablename__ == "gate_decisions"]
        assert [d.gate_name for d in decisions] == ["tier1", "dynamic"]

        boolean, shadow = decisions
        for decision in decisions:
            assert decision.gate_version == GATE_VERSION
            assert decision.story_id == sample_story.id
            assert decision.passed is True
            assert decision.tier1_unit_count == 2
            assert decision.distinct_owners == 2
            assert decision.owner_groups == {"BBC": 1, "Guardian": 1}
            assert [a["source_domain"] for a in decision.contributing_articles] == [
                "bbc.com",
                "theguardian.com",
            ]

        # The boolean gate has no score; the shadow row carries the score it would have used.
        assert (boolean.score, boolean.pass_threshold, boolean.breakdown) == (None, None, None)
        assert shadow.score == 70
        assert shadow.pass_threshold == 50
        assert shadow.breakdown["shadow"] is True
        assert shadow.breakdown["final_score"] == 70
        assert shadow.breakdown["passes"] is True
    finally:
        settings.dynamic_gate_enabled = original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])