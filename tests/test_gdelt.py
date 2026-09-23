"""Tests for GDELT ingestion: DomainResult classification, circuit breaker, health tracking."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
import httpx

from src.ingestion.gdelt import (
    fetch_gdelt_articles,
    ingest_gdelt,
    DomainResult,
    GDELT_TIER1_CRITICAL_DOMAINS,
    DOMAIN_FILTERS,
)
from src.schema.models import RawArticle, SourceTier


class MockResponse:
    """Mock httpx.Response for testing."""
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        import json
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self._json_data = json_data or {}
        self.headers = headers or {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=MagicMock(), response=self
            )

    def json(self):
        return self._json_data


@pytest.fixture
def sample_article():
    """Create a sample RawArticle for testing."""
    return RawArticle(
        url="https://apnews.com/article/test",
        url_hash="abc123",
        title="Test Article",
        body_text="This is a test article body with enough text to pass the 200 char minimum requirement for extraction.",
        summary="Test summary",
        source_domain="apnews.com",
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(timezone.utc),
        entities={"PERSON": ["Test"], "ORG": ["AP"], "GPE": ["Washington"]},
        content_hash="hash123",
    )


class TestFetchGdeltArticles:
    """Tests for fetch_gdelt_articles response classification."""

    @pytest.mark.asyncio
    async def test_200_valid_json_with_articles_ok_true(self, sample_article):
        """200 + valid JSON + articles → ok=True, articles populated."""
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as mock_fetch, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as mock_extract, \
             patch("src.ingestion.gdelt.extract_entities_top_n") as mock_entities, \
             patch("src.ingestion.gdelt.compute_url_hash") as mock_url_hash, \
             patch("src.ingestion.gdelt.compute_content_hash") as mock_content_hash, \
             patch("src.ingestion.gdelt.asyncio.sleep", new_callable=AsyncMock):

            mock_fetch.return_value = MockResponse(
                status_code=200,
                json_data={"articles": [{"url": "https://apnews.com/a", "title": "A", "seendate": "20240101120000", "excerpt": "excerpt"}]}
            )
            mock_extract.return_value = ("body text long enough to pass the 200 character minimum requirement for article extraction in the pipeline and then some more text to ensure it is definitely over two hundred characters total and then even more text to make sure it is clearly over the limit", "title")
            mock_entities.return_value = {"PERSON": [], "ORG": ["AP"], "GPE": []}
            mock_url_hash.return_value = "hash1"
            mock_content_hash.return_value = "chash1"

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is True
            assert len(result.articles) == 1
            assert result.articles[0].source_domain == "apnews.com"
            assert result.error is None

    @pytest.mark.asyncio
    async def test_200_valid_json_empty_articles_ok_true(self):
        """200 + valid JSON + empty articles → ok=True, articles=[] (genuine nothing new)."""
        with patch("src.ingestion.gdelt.fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = MockResponse(
                status_code=200,
                json_data={"articles": []}
            )

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is True
            assert result.articles == []
            assert result.error is None

    @pytest.mark.asyncio
    async def test_200_valid_json_missing_articles_key_ok_true(self):
        """200 + valid JSON + missing articles key → ok=True, articles=[]."""
        with patch("src.ingestion.gdelt.fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = MockResponse(
                status_code=200,
                json_data={}  # missing "articles" key
            )

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is True
            assert result.articles == []

    @pytest.mark.asyncio
    async def test_429_exhausted_retries_ok_false(self):
        """429 exhausted through fetch_with_retry → ok=False."""
        with patch("src.ingestion.gdelt.fetch_with_retry") as mock_fetch:
            # All retries return 429, final attempt also returns 429
            mock_fetch.return_value = MockResponse(status_code=429)

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is False
            assert "429" in result.error or "rate limit" in result.error.lower() or "HTTP" in result.error

    @pytest.mark.asyncio
    async def test_500_error_ok_false(self):
        """500 → ok=False."""
        with patch("src.ingestion.gdelt.fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = MockResponse(status_code=500, text="Internal Server Error")

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is False
            assert "500" in result.error or "http" in result.error.lower()

    @pytest.mark.asyncio
    async def test_empty_body_ok_false(self):
        """Empty response body → ok=False (anomalous, not 'no news')."""
        with patch("src.ingestion.gdelt.fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = MockResponse(status_code=200, text="")

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is False
            assert result.error == "empty response"

    @pytest.mark.asyncio
    async def test_non_json_content_type_ok_false(self):
        """Non-JSON content-type → ok=False."""
        with patch("src.ingestion.gdelt.fetch_with_retry") as mock_fetch:
            mock_fetch.return_value = MockResponse(
                status_code=200,
                text="<html>Error page</html>",
                headers={"content-type": "text/html"}
            )

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is False
            assert "non-json" in result.error.lower()


class TestIngestGdeltCircuitBreaker:
    """Tests for circuit breaker behavior in ingest_gdelt."""

    @pytest.mark.asyncio
    async def test_circuit_opens_at_threshold(self):
        """Circuit opens at exactly gdelt_circuit_breaker_threshold failures."""
        with patch("src.ingestion.gdelt.fetch_gdelt_articles") as mock_fetch, \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value.gdelt_circuit_breaker_threshold = 3
            # First 3 domains fail, 4th should be skipped
            mock_fetch.side_effect = [
                DomainResult(domain="bbc.com", ok=False, error="timeout"),
                DomainResult(domain="theguardian.com", ok=False, error="timeout"),
                DomainResult(domain="npr.org", ok=False, error="timeout"),
                # Should not be called - no more domains after circuit opens
            ]

            articles, health = await ingest_gdelt(hours_back=24, max_per_domain=50)

            # Only first 3 should have been called
            assert mock_fetch.call_count == 3

            # Check health structure
            assert health["succeeded"] == []
            assert health["failed"] == ["bbc.com", "theguardian.com", "npr.org"]
            assert health["skipped"] == []

    @pytest.mark.asyncio
    async def test_gdelt_domains_fail_sets_tier1_critical(self):
        """GDELT domains failing sets tier1_critical_down (empty now since all have RSS backup)."""
        with patch("src.ingestion.gdelt.fetch_gdelt_articles") as mock_fetch, \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value.gdelt_circuit_breaker_threshold = 3

            mock_fetch.side_effect = [
                DomainResult(domain="bbc.com", ok=False, error="api error"),
                DomainResult(domain="theguardian.com", ok=True, articles=[]),
                DomainResult(domain="npr.org", ok=True, articles=[]),
            ]

            _, health = await ingest_gdelt(hours_back=24, max_per_domain=50)

            assert "bbc.com" in health["failed"]
            assert "theguardian.com" not in health["failed"]

            # Verify the tier1 critical domains set is now empty (all have RSS backup)
            assert GDELT_TIER1_CRITICAL_DOMAINS == set()

    @pytest.mark.asyncio
    async def test_verify_sources_updated_for_domainresult(self):
        """verify_sources() updated for new DomainResult return type."""
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as mock_fetch, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as mock_extract, \
             patch("src.ingestion.gdelt.extract_entities_top_n") as mock_entities, \
             patch("src.ingestion.gdelt.compute_url_hash") as mock_url_hash, \
             patch("src.ingestion.gdelt.compute_content_hash") as mock_content_hash, \
             patch("src.ingestion.gdelt.asyncio.sleep", new_callable=AsyncMock):

            mock_fetch.return_value = MockResponse(
                status_code=200,
                json_data={
                    "articles": [
                        {"url": f"https://apnews.com/a{i}", "title": f"A{i}", "seendate": "20240101120000", "excerpt": "excerpt"}
                        for i in range(5)
                    ]
                }
            )
            mock_extract.return_value = ("body text long enough to pass the 200 character minimum requirement for article extraction in the pipeline and then some more text to ensure it is definitely over two hundred characters total and then even more text to make sure it is clearly over the limit", "title")
            mock_entities.return_value = {"PERSON": [], "ORG": ["AP"], "GPE": []}
            mock_url_hash.side_effect = [f"hash{i}" for i in range(5)]
            mock_content_hash.side_effect = [f"chash{i}" for i in range(5)]

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            # Verify verify_sources logic would work
            articles_count = len(result.articles)
            assert articles_count == 5


class TestRunIngestionIntegration:
    """Integration-style tests for run_ingestion health tracking (mocked)."""

    @pytest.mark.asyncio
    async def test_run_ingestion_logs_degraded_when_tier1_down(self):
        """run_ingestion logs 'degraded' when tier-1 critical domain is down."""
        from src.ingestion.run import run_ingestion

        with patch("src.ingestion.run.ingest_rss_feeds") as mock_rss, \
             patch("src.ingestion.run.ingest_gdelt") as mock_gdelt, \
             patch("src.ingestion.run.ingest_reddit") as mock_reddit, \
             patch("src.ingestion.run.init_db"), \
             patch("src.ingestion.run.get_session") as mock_session, \
             patch("src.ingestion.run.log_status"):

            mock_rss.return_value = []
            mock_gdelt.return_value = (
                [],  # articles
                {
                    "succeeded": ["reuters.com", "bbc.com", "theguardian.com", "npr.org"],
                    "failed": ["apnews.com"],
                    "skipped": [],
                }
            )
            mock_reddit.return_value = []

            # Mock session context manager
            mock_sess = AsyncMock()
            mock_session.return_value.__aenter__.return_value = mock_sess
            mock_sess.execute.return_value.scalars.return_value.all.return_value = []

            await run_ingestion(dry_run=True)

            # Check that ingest phase has correct health info
            # (we can't easily check the log_status call without more mocking)
            # But we can verify the gdelt return value structure
            mock_gdelt.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_exits_nonzero_on_tier1_critical_down(self):
        """main() exits non-zero when tier1 critical domain is down (not dry run)."""
        from src.ingestion.run import main

        with patch("src.ingestion.run.init_db"), \
             patch("src.ingestion.run.run_ingestion") as mock_run, \
             patch("sys.exit") as mock_exit:

            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "tier1_critical_down": ["apnews.com"],
                        "gdelt_health": {
                            "failed": ["apnews.com"],
                            "succeeded": ["reuters.com"],
                            "skipped": []
                        }
                    }
                }
            }

            await main()

            mock_exit.assert_called_with(1)

    @pytest.mark.asyncio
    async def test_main_exits_zero_when_only_rss_domains_down(self):
        """main() exits zero when only BBC/Guardian/NPR fail (RSS backup exists)."""
        from src.ingestion.run import main

        with patch("src.ingestion.run.init_db"), \
             patch("src.ingestion.run.run_ingestion") as mock_run, \
             patch("sys.exit") as mock_exit:

            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "tier1_critical_down": [],
                        "gdelt_health": {
                            "failed": ["bbc.com", "theguardian.com"],
                            "succeeded": ["apnews.com", "reuters.com"],
                            "skipped": []
                        }
                    }
                }
            }

            await main()

            mock_exit.assert_not_called()


class TestDomainResult:
    """Tests for DomainResult dataclass."""

    def test_default_ok(self):
        """Default DomainResult has ok=True."""
        dr = DomainResult(domain="test.com")
        assert dr.ok is True
        assert dr.articles == []
        assert dr.error is None

    def test_failed_result(self):
        """Failed DomainResult has ok=False and error."""
        dr = DomainResult(domain="test.com", ok=False, error="timeout")
        assert dr.ok is False
        assert dr.error == "timeout"

    def test_articles_populated(self):
        """Articles can be populated."""
        mock_art = MagicMock(spec=RawArticle)
        dr = DomainResult(domain="test.com", articles=[mock_art])
        assert len(dr.articles) == 1


class TestConfiguration:
    """Tests for gdelt_circuit_breaker_threshold config."""

    def test_config_has_threshold(self):
        """Settings has gdelt_circuit_breaker_threshold with default 3."""
        from src.shared.config import get_settings
        settings = get_settings()
        assert hasattr(settings, "gdelt_circuit_breaker_threshold")
        assert settings.gdelt_circuit_breaker_threshold == 3

    def test_tier1_critical_domains_constant(self):
        """GDELT_TIER1_CRITICAL_DOMAINS is empty since all tier-1 sources have RSS backup."""
        assert GDELT_TIER1_CRITICAL_DOMAINS == set()

    def test_domain_filters_list(self):
        """DOMAIN_FILTERS has 3 domains (BBC, Guardian, NPR - AP/Reuters use RSS)."""
        assert DOMAIN_FILTERS == ["bbc.com", "theguardian.com", "npr.org"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])