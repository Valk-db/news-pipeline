"""Tests for the GDELT DOC throttle defences: body detection, backoff, cache, breaker.

GDELT rate limits the DOC API with HTTP 200 plus a plain-text apology rather than
a 429, so these tests drive that exact shape: a 200 whose body is
"Please limit requests..." and whose content-type is text/plain.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

import httpx

from src.ingestion import gdelt
from src.ingestion.gdelt import (
    DOC_RATE_LIMITER,
    DOC_RESPONSE_CACHE,
    DOC_THROTTLE_CIRCUIT,
    DocResponseCache,
    GlobalRateLimiter,
    ThrottleCircuitBreaker,
    THROTTLE_BODY_BASE_DELAY,
    fetch_gdelt_articles,
    fetch_with_retry,
    is_throttle_response,
)
from src.schema.models import RawArticle, SourceTier


THROTTLE_BODY = "Please limit requests to one request every 5 seconds. Please do not hammer the service."


class FakeResponse:
    """Minimal stand-in for the httpx.Response surface this module touches."""

    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        import json as _json
        self.status_code = status_code
        self.text = text if text else _json.dumps(json_data if json_data is not None else {})
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=self)

    def json(self):
        return self._json_data


def throttled_response(body=THROTTLE_BODY):
    return FakeResponse(status_code=200, text=body, headers={"content-type": "text/plain"})


@pytest.fixture
def doc_payload():
    return FakeResponse(
        status_code=200,
        json_data={"articles": [{"url": "https://bbc.com/a", "title": "A", "seendate": "20240101120000"}]},
    )


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    """No test really waits: the backoff assertions read the recorded delays."""
    sleeps = AsyncMock()
    monkeypatch.setattr(gdelt.asyncio, "sleep", sleeps)
    return sleeps


@pytest.fixture(autouse=True)
def no_jitter(monkeypatch):
    """Backoff assertions read exact delays, so jitter is pinned to zero."""
    monkeypatch.setattr(gdelt.random, "uniform", lambda a, b: 0.0)


@pytest.fixture(autouse=True)
def idle_rate_limiter(monkeypatch):
    """Keep the shared limiter out of tests that are not about it.

    Every DOC request really does pass through the limiter; the limiter's own
    tests build a fresh instance so they measure real spacing.
    """
    monkeypatch.setattr(DOC_RATE_LIMITER, "acquire", AsyncMock())


def fake_client(*responses):
    client = AsyncMock()
    client.get.side_effect = list(responses)
    return client


def waits(sleeps):
    return [call.args[0] for call in sleeps.await_args_list]


def article(url="https://bbc.com/a"):
    return RawArticle(
        url=url, url_hash="h1", title="T", body_text="x" * 300,
        source_domain="bbc.com", source_tier=SourceTier.TIER1,
    )


class TestThrottleBodyDetection:
    """is_throttle_response: the check that must run before the JSON parse."""

    def test_detects_please_limit_requests(self):
        assert is_throttle_response(throttled_response()) is True

    def test_detection_is_case_insensitive(self):
        assert is_throttle_response(throttled_response("PLEASE LIMIT REQUESTS!!")) is True

    def test_detects_alternate_phrasings(self):
        for body in ("Please do not hammer the server", "Too Many Requests, sorry"):
            assert is_throttle_response(throttled_response(body)) is True

    def test_json_payload_is_never_scanned(self):
        # A real result whose article text mentions rate limiting must not trip us.
        response = FakeResponse(json_data={"articles": [{"title": "New rate limit policy announced"}]})
        assert is_throttle_response(response) is False

    def test_empty_body_is_not_a_throttle(self):
        assert is_throttle_response(FakeResponse(text="", headers={})) is False

    def test_other_plain_text_is_not_a_throttle(self):
        html = FakeResponse(text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"})
        assert is_throttle_response(html) is False

    def test_marker_beyond_the_scan_window_is_ignored(self):
        padding = "x" * (gdelt.THROTTLE_BODY_SCAN_CHARS + 10)
        assert is_throttle_response(throttled_response(padding + " Please limit requests")) is False


class TestBackoff:
    """Backoff size and shape for the two throttle signals."""

    @pytest.mark.asyncio
    async def test_body_throttle_retries_then_succeeds(self, doc_payload, instant_sleep):
        client = fake_client(throttled_response(), throttled_response(), doc_payload)

        response = await fetch_with_retry(client, "u", {}, max_retries=7, base_delay=10.0)

        assert response is doc_payload
        assert client.get.call_count == 3
        # Hammering during a body throttle extends it: 60s, then 120s, not the 10s base.
        assert waits(instant_sleep) == [THROTTLE_BODY_BASE_DELAY, THROTTLE_BODY_BASE_DELAY * 2]

    @pytest.mark.asyncio
    async def test_429_keeps_the_short_base_delay(self, instant_sleep):
        client = fake_client(FakeResponse(status_code=429), FakeResponse(json_data={"articles": []}))

        await fetch_with_retry(client, "u", {}, max_retries=7, base_delay=10.0)

        assert waits(instant_sleep) == [10.0]

    @pytest.mark.asyncio
    async def test_no_sleep_after_the_final_attempt(self, instant_sleep):
        client = fake_client(throttled_response())

        response = await fetch_with_retry(client, "u", {}, max_retries=0)

        assert client.get.call_count == 1
        assert instant_sleep.await_count == 0
        assert is_throttle_response(response) is True

    @pytest.mark.asyncio
    async def test_breaker_stops_the_backoff_mid_fetch(self, instant_sleep):
        # Seven retries are configured, but the third throttle opens the breaker,
        # so this fetch never sleeps out a fourth attempt it is not allowed to make.
        client = fake_client(*[throttled_response() for _ in range(7)])

        response = await fetch_with_retry(client, "u", {}, max_retries=7)

        assert client.get.call_count == gdelt.THROTTLE_CIRCUIT_THRESHOLD
        assert len(waits(instant_sleep)) == gdelt.THROTTLE_CIRCUIT_THRESHOLD - 1
        assert DOC_THROTTLE_CIRCUIT.is_open() is True
        assert is_throttle_response(response) is True

    @pytest.mark.asyncio
    async def test_jitter_is_added_to_the_wait(self, monkeypatch, instant_sleep):
        monkeypatch.setattr(gdelt.random, "uniform", lambda a, b: 1.5)
        client = fake_client(throttled_response(), FakeResponse(json_data={"articles": []}))

        await fetch_with_retry(client, "u", {}, max_retries=7)

        assert waits(instant_sleep) == [THROTTLE_BODY_BASE_DELAY + 1.5]

    @pytest.mark.asyncio
    async def test_every_attempt_goes_through_the_limiter(self):
        client = fake_client(throttled_response(), throttled_response(), FakeResponse(json_data={"articles": []}))

        await fetch_with_retry(client, "u", {}, max_retries=7)

        assert DOC_RATE_LIMITER.acquire.await_count == 3


class TestGlobalRateLimiter:
    """The DOC limit is per-IP, so one gate serves every domain."""

    @pytest.mark.asyncio
    async def test_second_call_waits_for_the_interval(self, monkeypatch, instant_sleep):
        monkeypatch.setattr(gdelt, "get_settings", lambda: type("S", (), {"gdelt_throttle_seconds": 30.0})())
        limiter = GlobalRateLimiter()

        await limiter.acquire()
        assert instant_sleep.await_count == 0  # nothing to wait for on a cold gate

        await limiter.acquire()

        assert 0 < instant_sleep.await_args.args[0] <= 30.0

    @pytest.mark.asyncio
    async def test_zero_override_skips_waiting_entirely(self, monkeypatch, instant_sleep):
        monkeypatch.setattr(gdelt, "get_settings", lambda: type("S", (), {"gdelt_throttle_seconds": 30.0})())
        limiter = GlobalRateLimiter()

        await limiter.acquire()
        await limiter.acquire(override=0)

        assert instant_sleep.await_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_callers_queue_behind_the_one_lock(self, instant_sleep):
        limiter = GlobalRateLimiter()

        await asyncio.gather(*(limiter.acquire(override=30.0) for _ in range(3)))

        # One lock held across the wait: the first caller goes straight out and the
        # other two wait for the interval. Per-domain spacing would let all three
        # hit GDELT in the same instant, which is exactly what gets an IP blocked.
        assert len(waits(instant_sleep)) == 2
        assert all(0 < w <= 30.0 for w in waits(instant_sleep))


class TestThrottleCircuitBreaker:
    """The breaker itself, and its cooldown."""

    def test_opens_on_the_third_consecutive_throttle(self):
        breaker = ThrottleCircuitBreaker()
        for _ in range(2):
            breaker.record_throttle()
        assert breaker.is_open() is False

        breaker.record_throttle()
        assert breaker.is_open() is True
        assert breaker.cooldown_remaining() == pytest.approx(15 * 60, abs=1)

    def test_one_success_clears_the_streak(self):
        breaker = ThrottleCircuitBreaker()
        breaker.record_throttle()
        breaker.record_throttle()
        breaker.record_success()
        breaker.record_throttle()

        assert breaker.is_open() is False
        assert breaker.consecutive_throttles == 1

    def test_expired_cooldown_reports_closed(self):
        breaker = ThrottleCircuitBreaker(cooldown_seconds=0.0)
        for _ in range(3):
            breaker.record_throttle()

        assert breaker.is_open() is False
        assert breaker.consecutive_throttles == 0

    def test_state_is_reportable(self):
        breaker = ThrottleCircuitBreaker()
        assert breaker.state() == "closed (0/3 throttles)"
        breaker.record_throttle()
        assert breaker.state() == "closed (1/3 throttles)"
        for _ in range(2):
            breaker.record_throttle()
        assert breaker.state().startswith("open")


class TestBreakerEndToEnd:
    """fetch_with_retry counts the throttles; fetch_gdelt_articles obeys the breaker."""

    @pytest.mark.asyncio
    async def test_throttles_then_success_leaves_the_breaker_closed(self, doc_payload):
        client = fake_client(throttled_response(), throttled_response(), doc_payload)

        await fetch_with_retry(client, "u", {}, max_retries=2)

        assert DOC_THROTTLE_CIRCUIT.is_open() is False
        assert DOC_THROTTLE_CIRCUIT.consecutive_throttles == 0

    @pytest.mark.asyncio
    async def test_three_body_throttles_open_the_circuit_and_stop_the_next_call(self):
        blocked = fake_client(throttled_response(), throttled_response(), throttled_response())

        await fetch_with_retry(blocked, "u", {}, max_retries=2)

        assert DOC_THROTTLE_CIRCUIT.is_open() is True

        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http:
            result = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)

        assert result.ok is False
        assert result.error == "circuit_open"
        assert http.await_count == 0  # no request at all while the breaker is open

    @pytest.mark.asyncio
    async def test_throttled_response_is_reported_as_throttled_not_as_a_parse_error(self):
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http:
            http.return_value = throttled_response()

            result = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)

        assert result.ok is False
        assert result.error.startswith("throttled:")
        assert "non-json" not in result.error


class TestResponseCache:
    """The 15 minute TTL cache on normalized query params."""

    def test_key_normalizes_case_and_www(self):
        assert DocResponseCache.make_key("  WWW.BBC.com ", 24, 10) == DocResponseCache.make_key("bbc.com", 24, 10)

    def test_key_separates_window_record_count_and_domain(self):
        key = DocResponseCache.make_key("bbc.com", 24, 10)
        assert DocResponseCache.make_key("bbc.com", 12, 10) != key
        assert DocResponseCache.make_key("bbc.com", 24, 50) != key
        assert DocResponseCache.make_key("npr.org", 24, 10) != key

    def test_default_ttl_matches_gdelt_refresh_cadence(self):
        assert DocResponseCache().ttl_seconds == 15 * 60

    def test_entry_expires_at_the_ttl(self):
        # A 50ms TTL stands in for the real 15 minutes. Real time, real clock: the
        # one thing not to do here is patch time.monotonic, which asyncio's loop
        # clock also reads.
        cache = DocResponseCache(ttl_seconds=0.05)
        key = cache.make_key("bbc.com", 24, 10)

        cache.put(key, [article()])
        assert len(cache.get(key)) == 1

        time.sleep(0.06)

        assert cache.get(key) is None

    def test_stored_list_is_copied_both_ways(self):
        cache = DocResponseCache()
        key = cache.make_key("bbc.com", 24, 10)
        stored = [article()]
        cache.put(key, stored)

        stored.append(article("https://bbc.com/b"))

        assert len(cache.get(key)) == 1


class TestCacheEndToEnd:
    """Cache hits must skip HTTP entirely, and failures must never be cached."""

    @pytest.mark.asyncio
    async def test_second_identical_call_is_a_cache_hit(self, doc_payload):
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as extract, \
             patch("src.ingestion.gdelt.extract_entities_top_n", return_value={}), \
             patch("src.ingestion.gdelt.compute_url_hash", return_value="h1"), \
             patch("src.ingestion.gdelt.compute_content_hash", return_value="c1"):
            http.return_value = doc_payload
            extract.return_value = ("body " * 100, "T")

            first = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)
            second = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)

        assert http.await_count == 1
        assert extract.await_count == 1
        assert first.ok is True and second.ok is True
        assert [a.url_hash for a in second.articles] == ["h1"]

    @pytest.mark.asyncio
    async def test_different_max_records_is_a_cache_miss(self, doc_payload):
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as extract:
            http.return_value = doc_payload
            extract.return_value = ("body " * 100, "T")

            await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)
            await fetch_gdelt_articles("bbc.com", 24, 50, throttle_seconds=0)

        assert http.await_count == 2

    @pytest.mark.asyncio
    async def test_expired_entry_refetches(self, doc_payload, monkeypatch):
        # A 50ms TTL stands in for the real 15 minutes; the wiring is what matters.
        monkeypatch.setattr(DOC_RESPONSE_CACHE, "ttl_seconds", 0.05)

        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http, \
             patch("src.ingestion.gdelt.extract_article", new_callable=AsyncMock) as extract:
            http.return_value = doc_payload
            extract.return_value = ("body " * 100, "T")

            await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)
            await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)
            assert http.await_count == 1

            time.sleep(0.06)  # past the TTL (asyncio.sleep is patched out here)
            await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)

        assert http.await_count == 2

    @pytest.mark.asyncio
    async def test_failures_are_not_cached(self):
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http:
            http.return_value = FakeResponse(status_code=500, text="Internal Server Error")

            first = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)
            second = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)

        assert first.ok is False and second.ok is False
        assert http.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_but_valid_result_is_cached(self):
        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http:
            http.return_value = FakeResponse(json_data={"articles": []})

            first = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)
            second = await fetch_gdelt_articles("bbc.com", 24, 10, throttle_seconds=0)

        assert first.ok is True and first.articles == []
        assert second.ok is True and second.articles == []
        assert http.await_count == 1


class TestIngestHealth:
    """The health dict must name the throttle instead of looking like an outage."""

    @pytest.mark.asyncio
    async def test_health_separates_throttled_domains_from_healthy_ones(self):
        ok_doc = FakeResponse(json_data={"articles": []})

        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http, \
             patch("src.ingestion.gdelt.fetch_gkg_geojson_articles", new_callable=AsyncMock) as gkg:
            http.side_effect = [ok_doc, throttled_response(), ok_doc]
            gkg.return_value = gdelt.DomainResult(domain="gkg-geojson", ok=False, error="empty")

            _, health = await gdelt.ingest_gdelt(hours_back=24, max_per_domain=10)

        assert health["succeeded"] == ["bbc.com", "npr.org"]
        assert health["failed"] == ["theguardian.com", "gkg-geojson"]  # nothing new, so the sweep ran and found nothing
        assert health["skipped"] == []

    @pytest.mark.asyncio
    async def test_open_circuit_lists_domains_as_skipped(self):
        # Three body throttles through the real retry path open the breaker.
        for _ in range(3):
            await fetch_with_retry(fake_client(throttled_response()), "u", {}, max_retries=0)

        with patch("src.ingestion.gdelt.fetch_with_retry", new_callable=AsyncMock) as http, \
             patch("src.ingestion.gdelt.fetch_gkg_geojson_articles", new_callable=AsyncMock) as gkg:
            gkg.return_value = gdelt.DomainResult(domain="gkg-geojson", ok=False, error="empty")

            _, health = await gdelt.ingest_gdelt(hours_back=24, max_per_domain=10)

        assert http.await_count == 0  # the whole domain sweep is skipped
        assert health["succeeded"] == []
        assert health["skipped"] == gdelt.DOMAIN_FILTERS
