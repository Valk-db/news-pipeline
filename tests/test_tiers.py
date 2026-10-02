"""Tests for tier classification and tier-1 gate logic."""

import uuid

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
    # The gate consumes one (unit_id, owner) pair per distinct tier-1 owner of each unit
    # (Corroboration.tier1_pairs): the unit half of the rule counts distinct unit ids, so two
    # articles inside one reporting unit are one unit's worth of corroboration.

    def test_passes_with_two_tier1_different_owners(self):
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "Reuters"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True
        assert "Gate passed" in reason

    def test_passes_with_three_tier1_two_owners(self):
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "Reuters"),
            (uuid.uuid4(), "BBC"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

    def test_fails_only_one_tier1_unit(self):
        units = [
            (uuid.uuid4(), "AP"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 unit" in reason

    def test_fails_two_tier1_same_owner(self):
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "AP"),  # Same owner group, two units
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "owner" in reason.lower()

    def test_fails_no_tier1_units(self):
        units = []
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason

    def test_fails_empty_list(self):
        units = []
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason

    def test_one_unit_two_owners_is_still_one_unit(self):
        # P0-4: one reporting event republished by two tier-1 outlets is one unit, not two.
        # tier1_pairs() emits one pair per (unit, owner), so the gate sees two pairs but one
        # distinct unit id and must block.
        unit_id = uuid.uuid4()
        units = [
            (unit_id, "AP"),
            (unit_id, "Reuters"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 unit" in reason

    def test_mixed_tiers_and_owners(self):
        # Two units, both tier-1 from AP: the units half passes, the owners half fails.
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "AP"),   # Two tier-1 units from AP
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False  # Only one distinct owner (AP) from tier-1
        assert "owner" in reason.lower()

    def test_get_owner_group_integration(self):
        # Verify the gate uses owner groups correctly
        units = [
            (uuid.uuid4(), get_owner_group("apnews.com")),
            (uuid.uuid4(), get_owner_group("reuters.com")),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

        units_same = [
            (uuid.uuid4(), get_owner_group("apnews.com")),
            (uuid.uuid4(), get_owner_group("www.apnews.com")),  # Same owner group
        ]
        should_queue, reason = evaluate_tier1_gate(units_same)
        assert should_queue is False