"""Tests for tier classification and tier-1 gate logic."""

import pytest
from src.verification.tiers import TIER1_DOMAINS, TIER2_DOMAINS, classify_source_tier
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