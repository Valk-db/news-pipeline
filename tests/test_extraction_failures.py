"""Tests for extraction failure reason codes."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from src.utils.trafilatura_extract import _extract_article_sync
from src.ingestion.rss import fetch_feed
from src.utils.ingest_stats import STATS


class TestTrafilaturaExtractionFailures:
    """Test that extraction failures are recorded with correct reason codes."""

    def setup_method(self):
        STATS.reset()

    def test_http_status_error_records_correct_code(self):
        """HTTP status errors record fetch_failed:http_<status>."""
        with patch("httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_get.side_effect = httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(), response=mock_response
            )

            body, title = _extract_article_sync(
                "https://example.com/article", source_key="test_source"
            )
            assert body is None
            assert title is None

            snap = STATS.snapshot()
            assert snap.get("test_source.fetch_failed:http_404") == 1

    def test_timeout_error_records_timeout(self):
        """Timeout errors record fetch_failed:timeout (regression test for B.1)."""
        with patch("httpx.get") as mock_get:
            mock_get.side_effect = httpx.TimeoutException("Read timeout")

            body, title = _extract_article_sync(
                "https://example.com/article", source_key="test_source"
            )
            assert body is None
            assert title is None

            snap = STATS.snapshot()
            assert snap.get("test_source.fetch_failed:timeout") == 1

    def test_generic_exception_records_error_type(self):
        """Generic exceptions record fetch_failed:error_<ExcType>."""
        with patch("httpx.get") as mock_get:
            mock_get.side_effect = ValueError("Invalid URL")

            body, title = _extract_article_sync(
                "https://example.com/article", source_key="test_source"
            )
            assert body is None
            assert title is None

            snap = STATS.snapshot()
            assert snap.get("test_source.fetch_failed:error_ValueError") == 1

    def test_empty_extract_records_empty_extract(self):
        """Empty extraction records fetch_failed:empty_extract."""
        with patch("httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.text = "<html><body>Too short</body></html>"
            mock_get.return_value = mock_response

            with patch("trafilatura.extract") as mock_extract:
                mock_extract.return_value = None

                body, title = _extract_article_sync(
                    "https://example.com/article", source_key="test_source"
                )
                assert body is None
                assert title is None

                snap = STATS.snapshot()
                assert snap.get("test_source.fetch_failed:empty_extract") == 1


class TestRssFeedFetchFailures:
    """Test RSS feed fetch failure reason codes."""

    @pytest.mark.asyncio
    async def test_feed_http_error_records_status(self):
        """Feed HTTP errors record feed_failed:http_<status>."""
        STATS.reset()

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=mock_response
        )

        result = await fetch_feed(mock_client, "https://example.com/feed", source_key="bbc")
        assert result is None

        snap = STATS.snapshot()
        assert snap.get("bbc.feed_failed:http_403") == 1

    @pytest.mark.asyncio
    async def test_feed_timeout_records_timeout(self):
        """Feed timeout errors record feed_failed:timeout (regression test for B.1)."""
        STATS.reset()

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.TimeoutException("Connect timeout")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await fetch_feed(mock_client, "https://example.com/feed", source_key="bbc")
            assert result is None
            # Verify sleep was called for retries
            assert mock_sleep.call_count >= 1

        snap = STATS.snapshot()
        assert snap.get("bbc.feed_failed:timeout") == 1

    @pytest.mark.asyncio
    async def test_feed_generic_error_records_error_type(self):
        """Feed generic errors record feed_failed:error_<ExcType>."""
        STATS.reset()

        mock_client = AsyncMock()
        mock_client.get.side_effect = ValueError("Invalid URL")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await fetch_feed(mock_client, "https://example.com/feed", source_key="bbc")
            assert result is None
            assert mock_sleep.call_count >= 1

        snap = STATS.snapshot()
        assert snap.get("bbc.feed_failed:error_ValueError") == 1

    @pytest.mark.asyncio
    async def test_feed_success_records_ok(self):
        """Successful feed fetch records feed_ok."""
        STATS.reset()

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.text = """<?xml version="1.0"?><rss><channel><item><title>Test</title><link>https://example.com/1</link></item></channel></rss>"""
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response

        with patch("feedparser.parse") as mock_parse:
            mock_parse.return_value = MagicMock(entries=[
                MagicMock(link="https://example.com/1", title="Test", published_parsed=(2024,1,1,0,0,0,0,0,0))
            ])

            result = await fetch_feed(mock_client, "https://example.com/feed", source_key="bbc")
            assert result is not None

        snap = STATS.snapshot()
        assert snap.get("bbc.feed_ok") == 1

    @pytest.mark.asyncio
    async def test_feed_403_not_retried(self):
        """403 is attempted exactly once (not retried)."""
        STATS.reset()

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=mock_response
        )

        result = await fetch_feed(mock_client, "https://example.com/feed", source_key="bbc")
        assert result is None

        # Should be called exactly once (no retries for 4xx other than 429)
        assert mock_client.get.call_count == 1

        snap = STATS.snapshot()
        assert snap.get("bbc.feed_failed:http_403") == 1

    @pytest.mark.asyncio
    async def test_feed_500_retried(self):
        """500 is retried up to max_retries times."""
        STATS.reset()

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.get.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error", request=MagicMock(), response=mock_response
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await fetch_feed(mock_client, "https://example.com/feed", source_key="bbc")
            assert result is None
            # Verify sleep was called for retries
            assert mock_sleep.call_count >= 1

        # Should be called max_retries times (default 3)
        assert mock_client.get.call_count == 3

        snap = STATS.snapshot()
        assert snap.get("bbc.feed_failed:http_500") == 1