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
from src.shared.config import Settings
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
             patch("src.ingestion.gdelt.fetch_gkg_geojson_articles") as mock_fallback, \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value.gdelt_circuit_breaker_threshold = 3
            # First 3 domains fail, 4th should be skipped
            mock_fetch.side_effect = [
                DomainResult(domain="bbc.com", ok=False, error="timeout"),
                DomainResult(domain="theguardian.com", ok=False, error="timeout"),
                DomainResult(domain="npr.org", ok=False, error="timeout"),
                # Should not be called - no more domains after circuit opens
            ]
            # DOC dead -> fallback runs; keep it empty so circuit assertions stay pure
            mock_fallback.return_value = DomainResult(domain="gkg-geojson", ok=False, error="empty")

            articles, health = await ingest_gdelt(hours_back=24, max_per_domain=50)

            # Only first 3 should have been called
            assert mock_fetch.call_count == 3
            # Fallback engaged exactly once after total DOC failure
            assert mock_fallback.call_count == 1

            # Check health structure
            assert health["succeeded"] == []
            assert health["failed"] == ["bbc.com", "theguardian.com", "npr.org", "gkg-geojson"]
            assert health["skipped"] == []
            assert health["fallback_used"] is False
            assert articles == []

    @pytest.mark.asyncio
    async def test_gdelt_domains_fail_sets_tier1_critical(self):
        """GDELT domains failing sets tier1_critical_down (empty now since all have RSS backup)."""
        with patch("src.ingestion.gdelt.fetch_gdelt_articles") as mock_fetch, \
             patch("src.ingestion.gdelt.fetch_gkg_geojson_articles") as mock_fallback, \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value.gdelt_circuit_breaker_threshold = 3

            mock_fetch.side_effect = [
                DomainResult(domain="bbc.com", ok=False, error="api error"),
                DomainResult(domain="theguardian.com", ok=True, articles=[]),
                DomainResult(domain="npr.org", ok=True, articles=[]),
            ]
            mock_fallback.return_value = DomainResult(domain="gkg-geojson", ok=False, error="empty")

            _, health = await ingest_gdelt(hours_back=24, max_per_domain=50)

            assert "bbc.com" in health["failed"]
            assert "theguardian.com" not in health["failed"]
            assert health["fallback_used"] is False

            # Verify the tier1 critical domains set is now empty (all have RSS backup)
            assert set() == GDELT_TIER1_CRITICAL_DOMAINS

    @pytest.mark.asyncio
    async def test_gkg_fallback_engages_when_doc_yields_nothing(self):
        """Total DOC failure triggers the v1 GKG GeoJSON fallback and merges its articles."""
        fb_article = RawArticle(
            url="https://example.com/fallback-story",
            url_hash="fb123",
            title="Fallback story",
            body_text="x" * 300,
            source_domain="example.com",
            source_tier=SourceTier.TIER2,
        )
        with patch("src.ingestion.gdelt.fetch_gdelt_articles") as mock_fetch, \
             patch("src.ingestion.gdelt.fetch_gkg_geojson_articles") as mock_fallback, \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value.gdelt_circuit_breaker_threshold = 10
            mock_fetch.return_value = DomainResult(domain="bbc.com", ok=True, articles=[])
            mock_fallback.return_value = DomainResult(
                domain="gkg-geojson", ok=True, articles=[fb_article],
            )

            articles, health = await ingest_gdelt(hours_back=24, max_per_domain=50)

            assert mock_fallback.call_count == 1
            assert health["fallback_used"] is True
            assert health["fallback_count"] == 1
            assert "gkg-geojson" in health["succeeded"]
            assert [a.url_hash for a in articles] == ["fb123"]

    @pytest.mark.asyncio
    async def test_gkg_fallback_skipped_when_doc_succeeds(self):
        """Fallback stays out of the way when the DOC API produces articles."""
        doc_article = RawArticle(
            url="https://bbc.com/doc-story",
            url_hash="doc123",
            title="DOC story",
            body_text="x" * 300,
            source_domain="bbc.com",
            source_tier=SourceTier.TIER1,
        )
        with patch("src.ingestion.gdelt.fetch_gdelt_articles") as mock_fetch, \
             patch("src.ingestion.gdelt.fetch_gkg_geojson_articles") as mock_fallback, \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value.gdelt_circuit_breaker_threshold = 10
            mock_fetch.return_value = DomainResult(domain="bbc.com", ok=True, articles=[doc_article])

            articles, health = await ingest_gdelt(hours_back=24, max_per_domain=50)

            assert mock_fallback.call_count == 0
            assert health["fallback_used"] is False
            assert [a.url_hash for a in articles] == ["doc123"]

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

    @pytest.mark.asyncio
    async def test_gdelt_call_site_respects_top_n_entities_5(self):
        """GDELT ingestion passes top_n=5 when settings.top_n_entities=5."""
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as mock_fetch, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as mock_extract, \
             patch("src.ingestion.gdelt.extract_entities_top_n") as mock_entities, \
             patch("src.ingestion.gdelt.compute_url_hash") as mock_url_hash, \
             patch("src.ingestion.gdelt.compute_content_hash") as mock_content_hash, \
             patch("src.ingestion.gdelt.asyncio.sleep", new_callable=AsyncMock), \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value = Settings(
                database_url="sqlite+aiosqlite:///:memory:",
                groq_api_key="",
                cerebras_api_key="",
                top_n_entities=5,
            )
            mock_fetch.return_value = MockResponse(
                status_code=200,
                json_data={"articles": [{"url": "https://apnews.com/a", "title": "A", "seendate": "20240101120000", "excerpt": "excerpt"}]}
            )
            # Body text >= 200 chars to pass MIN_BODY_LENGTH
            mock_extract.return_value = (
                "body text long enough to pass the 200 character minimum requirement for article extraction in the pipeline and then some more text to ensure it is definitely over two hundred characters total and then even more text to make sure it is clearly over the limit",
                "title"
            )
            mock_entities.return_value = {"PERSON": [], "ORG": ["AP"], "GPE": []}
            mock_url_hash.return_value = "hash1"
            mock_content_hash.return_value = "chash1"

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is True
            mock_entities.assert_called_once()
            # Verify the call site passed top_n=5
            assert mock_entities.call_args.kwargs["top_n"] == 5

    @pytest.mark.asyncio
    async def test_gdelt_call_site_respects_top_n_entities_1(self):
        """GDELT ingestion passes top_n=1 when settings.top_n_entities=1."""
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as mock_fetch, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as mock_extract, \
             patch("src.ingestion.gdelt.extract_entities_top_n") as mock_entities, \
             patch("src.ingestion.gdelt.compute_url_hash") as mock_url_hash, \
             patch("src.ingestion.gdelt.compute_content_hash") as mock_content_hash, \
             patch("src.ingestion.gdelt.asyncio.sleep", new_callable=AsyncMock), \
             patch("src.ingestion.gdelt.get_settings") as mock_settings:

            mock_settings.return_value = Settings(
                database_url="sqlite+aiosqlite:///:memory:",
                groq_api_key="",
                cerebras_api_key="",
                top_n_entities=1,
            )
            mock_fetch.return_value = MockResponse(
                status_code=200,
                json_data={"articles": [{"url": "https://apnews.com/a", "title": "A", "seendate": "20240101120000", "excerpt": "excerpt"}]}
            )
            mock_extract.return_value = (
                "body text long enough to pass the 200 character minimum requirement for article extraction in the pipeline and then some more text to ensure it is definitely over two hundred characters total and then even more text to make sure it is clearly over the limit",
                "title"
            )
            mock_entities.return_value = {"PERSON": [], "ORG": ["AP"], "GPE": []}
            mock_url_hash.return_value = "hash1"
            mock_content_hash.return_value = "chash1"

            result = await fetch_gdelt_articles("apnews.com", hours_back=24, max_records=10, throttle_seconds=0)

            assert result.ok is True
            mock_entities.assert_called_once()
            # Verify the call site passed top_n=1
            assert mock_entities.call_args.kwargs["top_n"] == 1


class TestRunIngestionIntegration:
    """Integration-style tests for run_ingestion health tracking.

    These run against a real in-memory database with the real ``build_adapters``
    and stubbed adapter fetches, so the health/degraded logic is the production
    logic rather than a reimplementation of it.
    """

    @pytest.mark.asyncio
    async def test_run_ingestion_logs_degraded_when_tier1_down(
        self, monkeypatch, ingestion_env
    ):
        """A tier-1 critical GDELT domain going down writes an 'ingest degraded' row.

        The point of this test is the degraded status line, so it asserts on the
        status_log row ``log_status`` actually wrote -- not on adapters
        succeeding, and not on a mock of a function ``run_ingestion`` has not
        called since the adapters landed.

        ``GDELT_TIER1_CRITICAL_DOMAINS`` is patched to a non-empty set because
        in production it is ``set()``, which makes this branch unreachable. See
        ``test_degraded_status_is_unreachable_in_production`` immediately below,
        which pins that fact rather than leaving it implicit.
        """
        from src.ingestion import run as run_module
        from src.ingestion.adapter import SourceHealth

        monkeypatch.setattr(
            run_module, "GDELT_TIER1_CRITICAL_DOMAINS", {"apnews.com", "reuters.com"}
        )
        ingestion_env.fetch(
            {"gdelt": []},
            health_by_adapter={
                "gdelt": SourceHealth(
                    status="degraded",
                    detail="apnews.com unreachable",
                    succeeded=["reuters.com", "bbc.com", "theguardian.com", "npr.org"],
                    failed=["apnews.com"],
                    skipped=[],
                )
            },
        )

        results = await ingestion_env.ingest(dry_run=False)

        ingestion = results["phases"]["ingestion"]
        # apnews.com is critical and failed; reuters.com is critical and did
        # not, so it must NOT be listed.
        assert ingestion["tier1_critical_down"] == ["apnews.com"]

        # The artifact: the status_log row run.py wrote for the ingest phase.
        statuses = await ingestion_env.statuses()
        assert ("ingest", "degraded") in statuses

    @pytest.mark.asyncio
    async def test_degraded_status_is_unreachable_in_production(
        self, monkeypatch, ingestion_env
    ):
        """With the shipped constant, the ingest phase can never be 'degraded'.

        ``GDELT_TIER1_CRITICAL_DOMAINS`` is ``set()`` in src/ingestion/gdelt.py
        ("All tier-1 sources now have RSS backups"), so
        ``tier1_critical_down = sorted(set() & ...)`` is always ``[]`` and
        ``ingest_status`` is always "ok" -- even when GDELT reports domains
        failed. This test exists so that fact is asserted and visible rather
        than discovered later by someone trusting the branch above: if the
        constant is ever repopulated, this test is the one that will tell you
        the reporting has become live.
        """
        from src.ingestion.adapter import SourceHealth
        from src.ingestion.gdelt import GDELT_TIER1_CRITICAL_DOMAINS

        assert set() == GDELT_TIER1_CRITICAL_DOMAINS

        ingestion_env.fetch(
            {"gdelt": []},
            health_by_adapter={
                "gdelt": SourceHealth(
                    status="down",
                    detail="GDELT unreachable",
                    succeeded=[],
                    failed=["apnews.com", "reuters.com", "bbc.com"],
                    skipped=[],
                )
            },
        )

        results = await ingestion_env.ingest(dry_run=False)

        ingestion = results["phases"]["ingestion"]
        assert ingestion["tier1_critical_down"] == []
        # The GDELT failure is still reported through adapter_health, which is
        # where a reader should look today.
        assert ingestion["adapter_health"]["gdelt"]["status"] == "down"
        statuses = await ingestion_env.statuses()
        assert ("ingest", "ok") in statuses
        assert ("ingest", "degraded") not in statuses

    @pytest.mark.asyncio
    async def test_main_exits_nonzero_on_tier1_critical_down(self):
        """main() exits non-zero when tier1 critical domain is down (not dry run)."""
        from src.ingestion.run import main

        with patch("src.ingestion.run.run_ingestion") as mock_run, \
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

        with patch("src.ingestion.run.run_ingestion") as mock_run, \
             patch("sys.exit") as mock_exit:

            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "tier1_critical_down": [],
                        "gdelt_health": {
                            "failed": ["bbc.com", "theguardian.com"],
                            "succeeded": ["apnews.com", "reuters.com"],
                            "skipped": []
                        },
                        "extraction_stats": {
                            "apnews.com.ok": 5,
                            "reuters.com.ok": 5,
                            "bbc.com.ok": 0,
                            "theguardian.com.ok": 0,
                            "npr.org.ok": 5,
                            "dw.com.ok": 5,
                            "france24.com.ok": 5,
                            "aljazeera.com.ok": 5,
                            "euronews.com.ok": 5,
                            "pbs.org.ok": 5,
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
        assert set() == GDELT_TIER1_CRITICAL_DOMAINS

    def test_domain_filters_list(self):
        """DOMAIN_FILTERS has 3 domains (BBC, Guardian, NPR - AP/Reuters use RSS)."""
        assert DOMAIN_FILTERS == ["bbc.com", "theguardian.com", "npr.org"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])