"""Tests for tier classification and tier-1 gate logic."""

import pytest
from unittest.mock import MagicMock
from src.verification.tiers import TIER1_DOMAINS, TIER2_DOMAINS, classify_source_tier, evaluate_tier1_gate
from src.schema.models import SourceTier, ReportingUnit


class TestClassifySourceTier:
    def test_tier1_domains(self):
        for domain in TIER1_DOMAINS:
            assert classify_source_tier(domain) == SourceTier.TIER1

    def test_tier2_domains(self):
        for domain in TIER2_DOMAINS:
            assert classify_source_tier(domain) == SourceTier.TIER2

    def test_unknown_domain_defaults_to_tier3(self):
        assert classify_source_tier("randomblog.com") == SourceTier.TIER3
        assert classify_source_tier("example.org") == SourceTier.TIER3

    def test_case_sensitive(self):
        assert classify_source_tier("BBC.COM") == SourceTier.TIER3
        assert classify_source_tier("ApNews.com") == SourceTier.TIER3


class TestTierConstants:
    def test_tier1_not_empty(self):
        assert len(TIER1_DOMAINS) > 0

    def test_tier2_not_empty(self):
        assert len(TIER2_DOMAINS) > 0

    def test_no_overlap(self):
        overlap = TIER1_DOMAINS & TIER2_DOMAINS
        assert overlap == set()

    def test_core_tier1_present(self):
        expected = {"bbc.com", "theguardian.com", "npr.org", "apnews.com", "reuters.com"}
        assert expected.issubset(TIER1_DOMAINS)


def make_unit(domain: str, tier: SourceTier) -> ReportingUnit:
    """Create a mock ReportingUnit for testing."""
    unit = MagicMock(spec=ReportingUnit)
    unit.source_domain = domain
    unit.source_tier = tier
    return unit


class TestEvaluateTier1Gate:
    def test_passes_with_two_tier1_different_owners(self):
        units = [
            make_unit("apnews.com", SourceTier.TIER1),
            make_unit("reuters.com", SourceTier.TIER1),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True
        assert "Gate passed" in reason

    def test_passes_with_three_tier1_two_owners(self):
        units = [
            make_unit("apnews.com", SourceTier.TIER1),
            make_unit("reuters.com", SourceTier.TIER1),
            make_unit("bbc.com", SourceTier.TIER1),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

    def test_fails_only_one_tier1_unit(self):
        units = [
            make_unit("apnews.com", SourceTier.TIER1),
            make_unit("reuters.com", SourceTier.TIER2),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 units" in reason

    def test_fails_two_tier1_same_owner(self):
        units = [
            make_unit("apnews.com", SourceTier.TIER1),
            make_unit("www.apnews.com", SourceTier.TIER1),  # Same owner group
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "owner" in reason.lower()

    def test_fails_no_tier1_units(self):
        units = [
            make_unit("nytimes.com", SourceTier.TIER2),
            make_unit("washingtonpost.com", SourceTier.TIER2),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason

    def test_fails_empty_list(self):
        units = []
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason

    def test_fails_tier3_only(self):
        units = [
            make_unit("randomblog.com", SourceTier.TIER3),
            make_unit("anotherblog.com", SourceTier.TIER3),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False