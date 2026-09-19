"""Tests for verification pipeline: units, stories, and tiers integration."""

import pytest
from src.verification.units import get_owner_group


class TestOwnershipGroups:
    def test_known_domains(self):
        assert get_owner_group("apnews.com") == "AP"
        assert get_owner_group("reuters.com") == "Reuters"
        assert get_owner_group("bbc.com") == "BBC"

    def test_subdomain_matching(self):
        assert get_owner_group("www.apnews.com") == "AP"
        assert get_owner_group("www.bbc.com") == "BBC"

    def test_unknown_domain(self):
        assert get_owner_group("randomblog.com") == "Independent"


class TestVerificationIntegration:
    """Integration tests requiring database - skipped in CI without real DB."""

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Requires PostgreSQL with ENUM types")
    async def test_build_reporting_units(self):
        pass

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Requires PostgreSQL with ENUM types")
    async def test_build_stories_groups_by_entities(self):
        pass


class TestTier1Gate:
    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Requires PostgreSQL with ENUM types")
    async def test_tier1_gate_passes_with_two_tier1_distinct_owners(self):
        pass