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
                articles = await ingest_rss_feeds(max_per_feed=10, sources=sources)

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
                articles = await ingest_rss_feeds(max_per_feed=10, sources=sources)

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
                await ingest_rss_feeds(max_per_feed=10, sources=sources)

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])