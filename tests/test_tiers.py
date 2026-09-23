"""Tests for tier classification and tier-1 gate logic."""

from src.verification.tiers import TIER1_DOMAINS, TIER2_DOMAINS, classify_source_tier, evaluate_tier1_gate
from src.verification.units import get_owner_group
from src.schema.models import SourceTier


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


class TestEvaluateTier1Gate:
    def test_passes_with_two_tier1_different_owners(self):
        # (tier, owner_group) tuples
        units = [
            ("tier1", "AP"),
            ("tier1", "Reuters"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True
        assert "Gate passed" in reason

    def test_passes_with_three_tier1_two_owners(self):
        units = [
            ("tier1", "AP"),
            ("tier1", "Reuters"),
            ("tier1", "BBC"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

    def test_fails_only_one_tier1_unit(self):
        units = [
            ("tier1", "AP"),
            ("tier2", "NYT"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 units" in reason

    def test_fails_two_tier1_same_owner(self):
        units = [
            ("tier1", "AP"),
            ("tier1", "AP"),  # Same owner group
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "owner" in reason.lower()

    def test_fails_no_tier1_units(self):
        units = [
            ("tier2", "NYT"),
            ("tier2", "WaPo"),
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
            ("tier3", "Independent"),
            ("tier3", "Independent"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False

    def test_mixed_tiers_and_owners(self):
        # Simulates a reporting unit with {"tier1": 2, "tier2": 1}
        units = [
            ("tier1", "AP"),
            ("tier1", "AP"),   # Two tier-1 from AP
            ("tier2", "NYT"),  # One tier-2 from NYT
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False  # Only one distinct owner (AP) from tier-1
        assert "owner" in reason.lower()

    def test_get_owner_group_integration(self):
        # Verify the gate uses owner groups correctly
        units = [
            ("tier1", get_owner_group("apnews.com")),
            ("tier1", get_owner_group("reuters.com")),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

        units_same = [
            ("tier1", get_owner_group("apnews.com")),
            ("tier1", get_owner_group("www.apnews.com")),  # Same owner group
        ]
        should_queue, reason = evaluate_tier1_gate(units_same)
        assert should_queue is False