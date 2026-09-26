"""Tests for per-(source, topic) reliability scoring (claim-consensus based)."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.verification.reliability import compute_source_topic_reliability
from src.schema.models import (
    Claim, ClaimStance, ClaimType, SourceTopicReliability, TopicGroup
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


@pytest.fixture
def sample_claim():
    """Create a sample claim."""
    return Claim(
        id=uuid.uuid4(),
        story_id=uuid.uuid4(),
        text="Test claim",
        claim_type=ClaimType.FACT,
        first_seen_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_topic_group():
    """Create a sample topic group."""
    return TopicGroup(
        id=uuid.uuid4(),
        name="Geopolitics > Middle East",
    )


@pytest.fixture
def mock_rows():
    """Create mock query rows."""
    return []


def make_evidence_row(claim_id, stance, confidence, topic_group_id, source_domain):
    """Helper to create a mock evidence row."""
    return (claim_id, stance, confidence, topic_group_id, source_domain)


@pytest.mark.asyncio
async def test_compute_reliability_basic(mock_session):
    """Test basic reliability scoring with agreeing sources."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()
    source_domain = "bbc.com"

    # Three SUPPORTS evidence rows -> majority SUPPORTS (meets MIN_SAMPLE_SIZE=3)
    rows = [
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 80, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 90, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 85, topic_group_id, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows

    # No existing reliability row (one per unique source+topic pair)
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [rows_result, existing_result, existing_result]

    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 1
    assert results["claims_considered"] == 1
    assert len(results["errors"]) == 0
    mock_session.add.assert_called_once()
    added_row = mock_session.add.call_args[0][0]
    assert isinstance(added_row, SourceTopicReliability)
    assert added_row.source_domain == source_domain
    assert added_row.topic_group_id == topic_group_id
    assert added_row.score == 100  # All agree
    assert added_row.sample_size == 3


@pytest.mark.asyncio
async def test_compute_reliability_disagreeing(mock_session):
    """Test reliability scoring with disagreeing source."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()
    source_domain = "unreliable.com"

    # Two SUPPORTS, one DISPUTES -> majority SUPPORTS (3 evidence >= MIN_SAMPLE_SIZE)
    rows = [
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 80, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 90, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.DISPUTES, 85, topic_group_id, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows
    mock_session.execute.return_value = rows_result

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    mock_session.execute.side_effect = [rows_result, existing_result]

    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 1
    assert results["claims_considered"] == 1
    mock_session.add.assert_called_once()
    added_row = mock_session.add.call_args[0][0]
    # SUPPORTS: 80+90=170, DISPUTES: 85 -> 170/255 = 66.6% -> 67
    assert added_row.score == 67


@pytest.mark.asyncio
async def test_compute_reliability_skips_single_evidence(mock_session):
    """Test that claims with only 1 non-neutral evidence are skipped."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()
    source_domain = "bbc.com"

    # Only one non-neutral evidence row -> no majority
    rows = [
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 80, topic_group_id, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows
    mock_session.execute.return_value = rows_result

    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 0
    assert results["claims_considered"] == 0
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_compute_reliability_skips_all_neutral(mock_session):
    """Test that claims with only neutral evidence are skipped."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()
    source_domain = "bbc.com"

    rows = [
        make_evidence_row(claim_id, ClaimStance.NEUTRAL, 50, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.NEUTRAL, 60, topic_group_id, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows
    mock_session.execute.return_value = rows_result

    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 0
    assert results["claims_considered"] == 0


@pytest.mark.asyncio
async def test_compute_reliability_below_min_sample_size(mock_session):
    """Test that pairs below MIN_SAMPLE_SIZE get no row."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()
    source_domain = "bbc.com"

    # Only 2 evidence rows total (< MIN_SAMPLE_SIZE=3)
    rows = [
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 80, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 90, topic_group_id, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows

    # No existing reliability row
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None

    mock_session.execute.side_effect = [rows_result, existing_result]
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    # 2 evidence rows < MIN_SAMPLE_SIZE (3), so should NOT score
    assert results["pairs_scored"] == 0
    assert results["claims_considered"] == 1
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_compute_reliability_updates_existing(mock_session):
    """Test that same-day re-run updates existing row."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()
    source_domain = "bbc.com"

    # 3 evidence rows to meet MIN_SAMPLE_SIZE
    rows = [
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 80, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 90, topic_group_id, source_domain),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 85, topic_group_id, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows

    # Existing row
    existing = SourceTopicReliability(
        source_domain=source_domain,
        topic_group_id=topic_group_id,
        score=50,
        sample_size=1,
        snapshot_date=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing

    mock_session.execute.side_effect = [rows_result, existing_result, existing_result]
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 1
    # Should update existing, not add new
    mock_session.add.assert_not_called()
    assert existing.score == 100  # Updated score
    assert existing.sample_size == 3  # Updated sample size


@pytest.mark.asyncio
async def test_compute_reliability_multiple_sources(mock_session):
    """Test multiple sources scored independently."""
    claim_id = uuid.uuid4()
    topic_group_id = uuid.uuid4()

    # Two sources: one agrees, one disagrees (3 evidence each >= MIN_SAMPLE_SIZE)
    rows = [
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 80, topic_group_id, "bbc.com"),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 90, topic_group_id, "bbc.com"),
        make_evidence_row(claim_id, ClaimStance.SUPPORTS, 85, topic_group_id, "bbc.com"),
        make_evidence_row(claim_id, ClaimStance.DISPUTES, 85, topic_group_id, "unreliable.com"),
        make_evidence_row(claim_id, ClaimStance.DISPUTES, 75, topic_group_id, "unreliable.com"),
        make_evidence_row(claim_id, ClaimStance.DISPUTES, 90, topic_group_id, "unreliable.com"),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None

    # 2 unique source+topic pairs = 2 existing checks
    mock_session.execute.side_effect = [rows_result, existing_result, existing_result]
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 2
    # bbc.com: 100% agree -> score 100
    # unreliable.com: 100% disagree -> score 0
    calls = mock_session.add.call_args_list
    assert len(calls) == 2
    scores = {c[0][0].source_domain: c[0][0].score for c in calls}
    assert scores["bbc.com"] == 100
    assert scores["unreliable.com"] == 0


@pytest.mark.asyncio
async def test_compute_reliability_different_topics(mock_session):
    """Test that different topics for same source are scored separately."""
    claim_id1 = uuid.uuid4()
    claim_id2 = uuid.uuid4()
    topic_group_id1 = uuid.uuid4()  # Geopolitics
    topic_group_id2 = uuid.uuid4()  # Economy
    source_domain = "bbc.com"

    # 3 evidence per topic (>= MIN_SAMPLE_SIZE)
    rows = [
        make_evidence_row(claim_id1, ClaimStance.SUPPORTS, 80, topic_group_id1, source_domain),
        make_evidence_row(claim_id1, ClaimStance.SUPPORTS, 90, topic_group_id1, source_domain),
        make_evidence_row(claim_id1, ClaimStance.SUPPORTS, 85, topic_group_id1, source_domain),
        make_evidence_row(claim_id2, ClaimStance.DISPUTES, 85, topic_group_id2, source_domain),
        make_evidence_row(claim_id2, ClaimStance.DISPUTES, 75, topic_group_id2, source_domain),
        make_evidence_row(claim_id2, ClaimStance.DISPUTES, 90, topic_group_id2, source_domain),
    ]

    mock_session.execute = AsyncMock()
    rows_result = MagicMock()
    rows_result.all.return_value = rows

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None

    # 2 unique source+topic pairs = 2 existing checks
    mock_session.execute.side_effect = [rows_result, existing_result, existing_result]
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    results = await compute_source_topic_reliability(mock_session, lookback_days=90)

    assert results["pairs_scored"] == 2
    calls = mock_session.add.call_args_list
    assert len(calls) == 2
    # Both topic groups should have rows
    topics = {c[0][0].topic_group_id for c in calls}
    assert topic_group_id1 in topics
    assert topic_group_id2 in topics


if __name__ == "__main__":
    pytest.main([__file__, "-v"])