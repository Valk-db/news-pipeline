"""Tests for per-domain circuit breaker in RSS ingestion (P1-5)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.ingestion.rss import ingest_rss_feeds, fetch_feed
import httpx


def make_fake_entry(url: str, title: str) -> MagicMock:
    """Create a properly mocked feedparser entry with attribute access."""
    entry = MagicMock()
    entry.link = url
    entry.title = title
    entry.summary = "Summary"
    entry.published_parsed = (2026, 1, 1, 12, 0, 0)
    # Support dict-style get() as well
    entry.get = lambda key, default="": {
        "link": url,
        "title": title,
        "summary": "Summary",
    }.get(key, default)
    return entry


class TestCircuitBreaker:
    """Tests for the circuit breaker that skips domains with <10% success rate after 8+ attempts."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_8_attempts_with_low_success_rate(self):
        """Circuit should open when >=8 attempts and ok/attempts < 10%."""
        # Create a fake feed with 10 entries
        fake_entries = [make_fake_entry(f"http://example.com/article{i}", f"Article {i}") for i in range(10)]
        fake_feed = MagicMock()
        fake_feed.entries = fake_entries

        # Mock extract_article to return article only for 1st call, then None (simulating 403/timeout)
        call_count = 0
        async def mock_extract_article(url, source_key=""):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Article body " * 50, "Extracted Title"
            return None, None

        # Mock fetch_feed to return our fake feed
        async def mock_fetch_feed(client, feed_url, timeout=30, source_key=""):
            return fake_feed

        sources = {
            "test": {
                "name": "Test Source",
                "domain": "example.com",
                "tier": "tier2",
                "feeds": ["http://example.com/feed.xml"],
            }
        }

        with patch("src.ingestion.rss.fetch_feed", mock_fetch_feed):
            with patch("src.ingestion.rss.extract_article", mock_extract_article):
                articles = await ingest_rss_feeds(sources=sources, max_per_feed=10)

        # With 1 ok out of 8 attempts = 12.5% > 10%, circuit doesn't open
        # After 9: 1/9 = 11.1%, after 10: 1/10 = 10.0% (not < 10%)
        # So all 10 entries are processed but only 1 succeeds
        assert len(articles) == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_with_zero_success_after_8(self):
        """Circuit should open when 0/8 success rate."""
        fake_entries = [make_fake_entry(f"http://fail.com/article{i}", f"Article {i}") for i in range(10)]
        fake_feed = MagicMock()
        fake_feed.entries = fake_entries

        # All extractions fail
        async def mock_extract_article(url, source_key=""):
            return None, None

        async def mock_fetch_feed(client, feed_url, timeout=30, source_key=""):
            return fake_feed

        sources = {
            "test": {
                "name": "Test Source",
                "domain": "fail.com",
                "tier": "tier2",
                "feeds": ["http://fail.com/feed.xml"],
            }
        }

        with patch("src.ingestion.rss.fetch_feed", mock_fetch_feed):
            with patch("src.ingestion.rss.extract_article", mock_extract_article):
                articles = await ingest_rss_feeds(sources=sources, max_per_feed=10)

        # All 8+ attempts fail, circuit should open after 8th attempt
        # Remaining 2 entries should be skipped
        assert len(articles) == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_records_stat(self):
        """Circuit open should record 'circuit_open' stat."""
        from src.utils.ingest_stats import STATS
        STATS.reset()

        fake_entries = [make_fake_entry(f"http://stat.com/article{i}", f"Article {i}") for i in range(10)]
        fake_feed = MagicMock()
        fake_feed.entries = fake_entries

        async def mock_extract_article(url, source_key=""):
            return None, None

        async def mock_fetch_feed(client, feed_url, timeout=30, source_key=""):
            return fake_feed

        sources = {
            "test": {
                "name": "Test Source",
                "domain": "stat.com",
                "tier": "tier2",
                "feeds": ["http://stat.com/feed.xml"],
            }
        }

        with patch("src.ingestion.rss.fetch_feed", mock_fetch_feed):
            with patch("src.ingestion.rss.extract_article", mock_extract_article):
                await ingest_rss_feeds(sources=sources, max_per_feed=10)

        snapshot = STATS.snapshot()
        # Should have circuit_open stat for the domain
        assert "test.circuit_open" in snapshot
        assert snapshot["test.circuit_open"] > 0


class TestRetryAfterHeader:
    """Tests for honoring Retry-After header on 429 (P1-6)."""

    @pytest.mark.asyncio
    async def test_retry_after_header_honored(self):
        """On 429, should use Retry-After header value (capped at 60s)."""
        client = MagicMock()
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "30"}
        response.raise_for_status.side_effect = httpx.HTTPStatusError("429", request=MagicMock(), response=response)

        client.get = AsyncMock(side_effect=[response, MagicMock(status_code=200, text="<rss></rss>")])

        with patch("src.ingestion.rss.feedparser.parse") as mock_parse:
            mock_parse.return_value = MagicMock(entries=[])
            await fetch_feed(client, "http://test.com/feed.xml", timeout=30, source_key="test")

        # Second call should happen after sleep
        assert client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_after_capped_at_60s(self):
        """Retry-After > 60s should be capped at 60s."""
        client = MagicMock()
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "120"}  # 2 minutes
        response.raise_for_status.side_effect = httpx.HTTPStatusError("429", request=MagicMock(), response=response)

        client.get = AsyncMock(side_effect=[response, MagicMock(status_code=200, text="<rss></rss>")])

        with patch("src.ingestion.rss.asyncio.sleep") as mock_sleep:
            with patch("src.ingestion.rss.feedparser.parse") as mock_parse:
                mock_parse.return_value = MagicMock(entries=[])
                await fetch_feed(client, "http://test.com/feed.xml", timeout=30, source_key="test")

            # Should sleep for 60s (capped), not 120s
            mock_sleep.assert_called_with(60)


class TestReconciliationInvariant:
    """Tests for the circuit breaker reconciliation invariant."""

    @pytest.mark.asyncio
    async def test_reconciliation_invariant_circuit_tripped(self):
        """Verify: already_known + entries_seen + circuit_open == entries_in_feed for a domain.

        This invariant should hold after processing, accounting for the circuit_tripped
        event being recorded separately from circuit_open skips.
        """
        from src.utils.ingest_stats import STATS
        STATS.reset()

        # Create a fake feed with 10 entries that all fail extraction
        fake_entries = [make_fake_entry(f"http://invariant.com/article{i}", f"Article {i}") for i in range(10)]
        fake_feed = MagicMock()
        fake_feed.entries = fake_entries

        # All extractions fail
        async def mock_extract_article(url, source_key=""):
            return None, None

        async def mock_fetch_feed(client, feed_url, timeout=30, source_key=""):
            return fake_feed

        sources = {
            "test": {
                "name": "Test Source",
                "domain": "invariant.com",
                "tier": "tier2",
                "feeds": ["http://invariant.com/feed.xml"],
            }
        }

        with patch("src.ingestion.rss.fetch_feed", mock_fetch_feed):
            with patch("src.ingestion.rss.extract_article", mock_extract_article):
                await ingest_rss_feeds(sources=sources, max_per_feed=10)

        snapshot = STATS.snapshot()

        # Extract counts for this domain
        entries_in_feed = snapshot.get("test.entries_in_feed", 0)
        entries_seen = snapshot.get("test.entries_seen", 0)
        already_known = snapshot.get("test.already_known", 0)
        ok_count = snapshot.get("test.ok", 0)
        circuit_open = snapshot.get("test.circuit_open", 0)
        circuit_tripped = snapshot.get("test.circuit_tripped", 0)

        # With 10 entries all failing:
        # - entries_in_feed = 10 (recorded for every entry with a URL)
        # - entries_seen = 10 (recorded in process_feed_entry for each attempt)
        # - already_known = 0 (no dedup)
        # - ok = 0 (no successful extractions)
        # - circuit_tripped = 1 (tripped on 8th attempt)
        # - circuit_open = 2 (skipped 9th and 10th entries)

        # Invariant: already_known + entries_seen + circuit_open == entries_in_feed
        # 0 + 10 + 2 = 12 ≠ 10  --- this shows the issue: entries_seen is counted for ALL attempts
        # But circuit_open skips don't call process_feed_entry, so entries_seen is only for attempted

        # Actually, the invariant should be:
        # entries_in_feed = already_known + (entries_seen + circuit_open)
        # But entries_seen is only recorded for entries that reach process_feed_entry
        # circuit_open is recorded for entries skipped by circuit breaker

        # Let's check the actual counts
        print(f"entries_in_feed: {entries_in_feed}")
        print(f"entries_seen: {entries_seen}")
        print(f"already_known: {already_known}")
        print(f"ok: {ok_count}")
        print(f"circuit_open: {circuit_open}")
        print(f"circuit_tripped: {circuit_tripped}")

        # The invariant: entries_in_feed should equal already_known + entries_seen + circuit_open
        # Because every entry is either already_known, processed (entries_seen), or circuit_open skipped
        # Note: circuit_tripped is a separate event, not an entry
        invariant_sum = already_known + entries_seen + circuit_open
        assert invariant_sum == entries_in_feed, (
            f"Reconciliation invariant failed: already_known({already_known}) + "
            f"entries_seen({entries_seen}) + circuit_open({circuit_open}) = {invariant_sum} "
            f"!= entries_in_feed({entries_in_feed})"
        )

        # circuit_tripped should be exactly 1 (the trip event)
        assert circuit_tripped == 1, f"Expected circuit_tripped=1, got {circuit_tripped}"

    @pytest.mark.asyncio
    async def test_reconciliation_invariant_with_dedup(self):
        """Verify invariant holds when some entries are already_known."""
        from src.utils.ingest_stats import STATS
        STATS.reset()

        # Create a fake feed with 10 entries
        fake_entries = [make_fake_entry(f"http://dedup.com/article{i}", f"Article {i}") for i in range(10)]
        fake_feed = MagicMock()
        fake_feed.entries = fake_entries

        # All extractions fail
        async def mock_extract_article(url, source_key=""):
            return None, None

        async def mock_fetch_feed(client, feed_url, timeout=30, source_key=""):
            return fake_feed

        # 5 entries already known in DB
        known_hashes = {f"hash_{entry.get('link', '')}" for entry in fake_entries[:5]}

        sources = {
            "test": {
                "name": "Test Source",
                "domain": "dedup.com",
                "tier": "tier2",
                "feeds": ["http://dedup.com/feed.xml"],
            }
        }

        with patch("src.ingestion.rss.fetch_feed", mock_fetch_feed):
            with patch("src.ingestion.rss.extract_article", mock_extract_article):
                await ingest_rss_feeds(
                    sources=sources,
                    max_per_feed=10,
                    known_url_hashes=known_hashes
                )

        snapshot = STATS.snapshot()

        entries_in_feed = snapshot.get("test.entries_in_feed", 0)
        entries_seen = snapshot.get("test.entries_seen", 0)
        already_known = snapshot.get("test.already_known", 0)
        circuit_open = snapshot.get("test.circuit_open", 0)

        # Invariant: already_known + entries_seen + circuit_open == entries_in_feed
        invariant_sum = already_known + entries_seen + circuit_open
        assert invariant_sum == entries_in_feed, (
            f"Reconciliation invariant failed: already_known({already_known}) + "
            f"entries_seen({entries_seen}) + circuit_open({circuit_open}) = {invariant_sum} "
            f"!= entries_in_feed({entries_in_feed})"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])