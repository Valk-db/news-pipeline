"""Tests for classify_tier1_sources_broken function."""

import pytest
from src.ingestion.run import classify_tier1_sources_broken
from src.ingestion.source_registry import SourceTier


class MockSourceConfig:
    """Mock source config from source_registry."""
    def __init__(self, domain, name, tier):
        self.domain = domain
        self.name = name
        self.tier = tier


class TestClassifyBroken:
    """Tests for tier-1 source BROKEN classification."""

    def make_tier1_sources(self, domains):
        """Create mock tier1_sources dict."""
        return {
            domain: MockSourceConfig(domain, domain, SourceTier.TIER1)
            for domain in domains
        }

    def test_all_ok_sources_not_broken(self):
        """All sources with ok > 0 should not be broken."""
        stats = {
            "bbc.com.entries_in_feed": 50,
            "bbc.com.entries_seen": 50,
            "bbc.com.ok": 50,
            "bbc.com.already_known": 0,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
            "theguardian.com.entries_in_feed": 40,
            "theguardian.com.entries_seen": 40,
            "theguardian.com.ok": 40,
            "theguardian.com.already_known": 0,
            "theguardian.com.feed_ok": 2,
            "theguardian.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com", "theguardian.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        assert broken == []
        assert breakdown["bbc.com"]["is_broken"] is False
        assert breakdown["theguardian.com"]["is_broken"] is False

    def test_entries_in_feed_but_zero_ok_broken(self):
        """entries_in_feed > 0 but ok+already_known == 0 -> BROKEN."""
        stats = {
            "bbc.com.entries_in_feed": 50,
            "bbc.com.entries_seen": 0,
            "bbc.com.ok": 0,
            "bbc.com.already_known": 0,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
            "theguardian.com.entries_in_feed": 40,
            "theguardian.com.entries_seen": 40,
            "theguardian.com.ok": 40,
            "theguardian.com.already_known": 0,
            "theguardian.com.feed_ok": 2,
            "theguardian.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com", "theguardian.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        assert broken == ["bbc.com"]
        assert breakdown["bbc.com"]["is_broken"] is True
        assert "extraction failed" in breakdown["bbc.com"]["reason"]
        assert breakdown["theguardian.com"]["is_broken"] is False

    def test_all_feeds_failed_broken(self):
        """All feeds failed (feed_failed > 0, feed_ok == 0) -> BROKEN."""
        stats = {
            "bbc.com.entries_in_feed": 0,
            "bbc.com.entries_seen": 0,
            "bbc.com.ok": 0,
            "bbc.com.already_known": 0,
            "bbc.com.feed_ok": 0,
            "bbc.com.feed_failed": 3,
            "theguardian.com.entries_in_feed": 40,
            "theguardian.com.entries_seen": 40,
            "theguardian.com.ok": 40,
            "theguardian.com.already_known": 0,
            "theguardian.com.feed_ok": 2,
            "theguardian.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com", "theguardian.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        assert broken == ["bbc.com"]
        assert breakdown["bbc.com"]["is_broken"] is True
        assert "all feeds failed" in breakdown["bbc.com"]["reason"]
        assert breakdown["theguardian.com"]["is_broken"] is False

    def test_feeds_ok_but_zero_entries_in_feed_while_others_have_entries_broken(self):
        """feeds OK but zero entries_in_feed while other tier-1 have entries_in_feed > 0 -> BROKEN."""
        stats = {
            "bbc.com.entries_in_feed": 0,
            "bbc.com.entries_seen": 0,
            "bbc.com.ok": 0,
            "bbc.com.already_known": 0,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
            "theguardian.com.entries_in_feed": 40,
            "theguardian.com.entries_seen": 40,
            "theguardian.com.ok": 40,
            "theguardian.com.already_known": 0,
            "theguardian.com.feed_ok": 2,
            "theguardian.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com", "theguardian.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        assert broken == ["bbc.com"]
        assert breakdown["bbc.com"]["is_broken"] is True
        assert "zero entries_in_feed while other tier-1 sources have entries_in_feed > 0" in breakdown["bbc.com"]["reason"]
        assert breakdown["theguardian.com"]["is_broken"] is False

    def test_feeds_ok_but_zero_entries_when_others_also_zero_not_broken(self):
        """feeds OK but zero entries_in_feed, AND no other tier-1 has entries_in_feed -> NOT broken."""
        stats = {
            "bbc.com.entries_in_feed": 0,
            "bbc.com.entries_seen": 0,
            "bbc.com.ok": 0,
            "bbc.com.already_known": 0,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
            "theguardian.com.entries_in_feed": 0,
            "theguardian.com.entries_seen": 0,
            "theguardian.com.ok": 0,
            "theguardian.com.already_known": 0,
            "theguardian.com.feed_ok": 2,
            "theguardian.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com", "theguardian.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        # Both have zero entries_in_feed, but since NO other tier-1 has entries_in_feed,
        # neither is broken per condition 3 (but they might be broken per other conditions)
        assert breakdown["bbc.com"]["is_broken"] is False
        assert breakdown["theguardian.com"]["is_broken"] is False
        # Actually, with entries_in_feed=0, ok=0, already_known=0, condition 1 doesn't apply
        # feed_ok > 0 so condition 2 doesn't apply
        # sources_with_entries is empty so condition 3 doesn't apply
        # So both are OK (this is an all-dupes or all-known scenario)

    def test_npr_case_not_broken(self):
        """NPR case: feed_ok>0, entries_in_feed>0, already_known>0, ok=0, entries_seen=0 -> NOT broken."""
        stats = {
            "npr.org.entries_in_feed": 30,
            "npr.org.entries_seen": 0,
            "npr.org.ok": 0,
            "npr.org.already_known": 30,
            "npr.org.feed_ok": 3,
            "npr.org.feed_failed": 0,
            "bbc.com.entries_in_feed": 13,
            "bbc.com.entries_seen": 13,
            "bbc.com.ok": 13,
            "bbc.com.already_known": 97,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["npr.org", "bbc.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        # NPR has entries_in_feed > 0 and already_known > 0, so ok+already_known != 0 -> NOT broken
        assert breakdown["npr.org"]["is_broken"] is False
        assert breakdown["bbc.com"]["is_broken"] is False

    def test_already_known_counts_as_success(self):
        """already_known should count as success (not broken)."""
        stats = {
            "bbc.com.entries_in_feed": 100,
            "bbc.com.entries_seen": 0,
            "bbc.com.ok": 0,
            "bbc.com.already_known": 100,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        assert broken == []
        assert breakdown["bbc.com"]["is_broken"] is False

    def test_mixed_some_broken_some_ok(self):
        """Multiple sources, some broken some ok."""
        stats = {
            "bbc.com.entries_in_feed": 50,
            "bbc.com.entries_seen": 50,
            "bbc.com.ok": 50,
            "bbc.com.already_known": 0,
            "bbc.com.feed_ok": 3,
            "bbc.com.feed_failed": 0,
            "theguardian.com.entries_in_feed": 40,
            "theguardian.com.entries_seen": 0,
            "theguardian.com.ok": 0,
            "theguardian.com.already_known": 0,
            "theguardian.com.feed_ok": 2,
            "theguardian.com.feed_failed": 0,
            "npr.org.entries_in_feed": 0,
            "npr.org.entries_seen": 0,
            "npr.org.ok": 0,
            "npr.org.already_known": 0,
            "npr.org.feed_ok": 0,
            "npr.org.feed_failed": 3,
        }
        tier1_sources = self.make_tier1_sources(["bbc.com", "theguardian.com", "npr.org"])

        broken, breakdown = classify_tier1_sources_broken(stats, tier1_sources)

        assert set(broken) == {"theguardian.com", "npr.org"}
        assert breakdown["bbc.com"]["is_broken"] is False
        assert breakdown["theguardian.com"]["is_broken"] is True
        assert breakdown["npr.org"]["is_broken"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])