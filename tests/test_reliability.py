"""Tests for reliability scoring functionality."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.schema.models import (
    SourceReliabilitySnapshot,
    FactCheckRecord,
    CorrectionRecord,
)
from src.shared.llm import LLMClient


def test_reliability_models_exist():
    """Test that all reliability models are defined."""
    # SourceReliabilitySnapshot
    snapshot = SourceReliabilitySnapshot(
        source_domain="example.com",
        snapshot_date=datetime.now(timezone.utc),
        factual_accuracy=85,
        correction_rate=2,
        consensus_alignment=78,
        transparency_score=90,
        reliability_score=83,
        total_claims_verified=100,
        claims_true=85,
        claims_false=10,
        claims_mixed=5,
        corrections_count=2,
        articles_sampled=50,
        tier_at_snapshot="tier1",
    )
    assert snapshot.reliability_score == 83
    assert snapshot.source_domain == "example.com"

    # FactCheckRecord
    fc = FactCheckRecord(
        id=uuid.uuid4(),
        source_domain="example.com",
        article_id=uuid.uuid4(),
        claim="The economy grew by 5% last quarter",
        claim_hash="abc123",
        verdict=FactCheckRecord.Verdict.TRUE,
        confidence=90,
        fact_checker=FactCheckRecord.FactChecker.LLM_VERIFIER,
        explanation="Verified against official statistics",
    )
    assert fc.verdict == FactCheckRecord.Verdict.TRUE
    assert fc.confidence == 90

    # CorrectionRecord
    correction = CorrectionRecord(
        id=uuid.uuid4(),
        source_domain="example.com",
        article_id=uuid.uuid4(),
        original_text="The economy grew by 5%",
        corrected_text="The economy grew by 3%",
        severity=CorrectionRecord.Severity.MODERATE,
        correction_date=datetime.now(timezone.utc),
        correction_url="https://example.com/correction",
    )
    assert correction.severity == CorrectionRecord.Severity.MODERATE


def test_fact_check_verdicts():
    """Test FactCheckRecord verdict enum values."""
    assert FactCheckRecord.Verdict.TRUE == "true"
    assert FactCheckRecord.Verdict.MOSTLY_TRUE == "mostly_true"
    assert FactCheckRecord.Verdict.MIXED == "mixed"
    assert FactCheckRecord.Verdict.MOSTLY_FALSE == "mostly_false"
    assert FactCheckRecord.Verdict.FALSE == "false"
    assert FactCheckRecord.Verdict.UNVERIFIED == "unverified"


def test_fact_checker_types():
    """Test FactCheckRecord fact_checker enum values."""
    assert FactCheckRecord.FactChecker.CLAIMBUSTER == "claimbuster"
    assert FactCheckRecord.FactChecker.LLM_VERIFIER == "llm_verifier"
    assert FactCheckRecord.FactChecker.CLAIMREVIEW == "claimreview"
    assert FactCheckRecord.FactChecker.MANUAL == "manual"


def test_correction_severities():
    """Test CorrectionRecord severity enum values."""
    assert CorrectionRecord.Severity.MINOR == "minor"
    assert CorrectionRecord.Severity.MODERATE == "moderate"
    assert CorrectionRecord.Severity.MAJOR == "major"
    assert CorrectionRecord.Severity.RETRACTION == "retraction"


def test_fact_checker_imports():
    """Test fact checker classes can be imported."""
    from src.reliability.fact_checker import (
        FactChecker,
        Claim,
        extract_claims_from_article,
        fact_check_article,
    )
    assert FactChecker is not None
    assert Claim is not None
    assert callable(extract_claims_from_article)
    assert callable(fact_check_article)


def test_consensus_analyzer_imports():
    """Test consensus analyzer can be imported."""
    from src.reliability.consensus_analyzer import (
        ConsensusAnalyzer,
        compute_daily_reliability_snapshots,
        detect_corrections,
    )
    assert ConsensusAnalyzer is not None
    assert callable(compute_daily_reliability_snapshots)
    assert callable(detect_corrections)


@pytest.mark.asyncio
async def test_fact_checker_basic():
    """Test basic FactChecker functionality."""
    from src.reliability.fact_checker import FactChecker, Claim

    checker = FactChecker()

    claim = Claim(
        text="The Earth orbits the Sun",
        claim_hash="test_hash_1",
        entities=["Earth", "Sun"],
        position=100,
        context="Scientists confirm the Earth orbits the Sun",
        claim_type="definition",
    )

    # Test with mocked LLM - patch the LLMClient.chat_completion method directly
    with patch.object(LLMClient, 'chat_completion', new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"verdict": "true", "confidence": 95, "explanation": "Basic astronomy fact", "evidence_needed": "None"}'}}]
        }

        result = await checker.check_claim(claim, "example.com")

        assert result["verdict"] == FactCheckRecord.Verdict.TRUE
        assert result["confidence"] == 95
        assert result["fact_checker"] == FactCheckRecord.FactChecker.LLM_VERIFIER


@pytest.mark.asyncio
async def test_aggregate_results():
    """Test result aggregation in FactChecker."""
    from src.reliability.fact_checker import FactChecker

    checker = FactChecker()

    results = [
        {"verdict": FactCheckRecord.Verdict.TRUE, "confidence": 90, "fact_checker": FactCheckRecord.FactChecker.LLM_VERIFIER},
        {"verdict": FactCheckRecord.Verdict.MOSTLY_TRUE, "confidence": 80, "fact_checker": FactCheckRecord.FactChecker.LLM_VERIFIER},
    ]

    aggregated = checker._aggregate_results(results)

    assert aggregated["verdict"] in [FactCheckRecord.Verdict.TRUE, FactCheckRecord.Verdict.MOSTLY_TRUE]
    assert 80 <= aggregated["confidence"] <= 90


@pytest.mark.asyncio
async def test_consensus_analyzer_basic():
    """Test ConsensusAnalyzer initialization."""
    from src.reliability.consensus_analyzer import ConsensusAnalyzer

    analyzer = ConsensusAnalyzer()
    assert analyzer is not None
    assert analyzer.embedding_service is not None


@pytest.mark.asyncio
async def test_compute_transparency_score():
    """Test transparency score computation."""
    from src.reliability.consensus_analyzer import _compute_transparency_score

    # Known high-transparency domains
    assert _compute_transparency_score("apnews.com") == 100
    assert _compute_transparency_score("reuters.com") == 100
    assert _compute_transparency_score("bbc.com") == 90
    assert _compute_transparency_score("theguardian.com") == 90

    # Unknown domain gets default
    assert _compute_transparency_score("unknown.com") == 40


@pytest.mark.asyncio
async def test_texts_differ_significantly():
    """Test significant text difference detection."""
    from src.reliability.consensus_analyzer import _texts_differ_significantly

    # Same text
    assert not _texts_differ_significantly("Hello world", "Hello world")

    # Minor whitespace
    assert not _texts_differ_significantly("Hello  world", "Hello world")

    # Significant change
    assert _texts_differ_significantly("The economy grew by 5%", "The economy grew by 3%")

    # Small change
    assert not _texts_differ_significantly("The economy grew by 5%", "The economy grew by 5.1%")


@pytest.mark.asyncio
async def test_assess_correction_severity():
    """Test correction severity assessment."""
    from src.reliability.consensus_analyzer import _assess_correction_severity

    old = "The economy grew by 5% last quarter."
    new_minor = "The economy grew by 5% last quarter."  # Same
    new_moderate = "The economy grew by 3% last quarter."
    new_major = "The economy shrank by 2% last quarter after revised data showed different results."

    assert _assess_correction_severity(old, new_minor) == "minor"
    assert _assess_correction_severity(old, new_moderate) in ["moderate", "minor"]
    # Major corrections with numerical changes + significant text changes
    assert _assess_correction_severity(old, new_major) in ["major", "moderate"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])