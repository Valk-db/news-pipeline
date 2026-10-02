"""Tests for RSS fetch tuning: concurrency semaphore and per-feed timeout.

The production defaults (10 concurrent fetches, 30s timeout) are pinned here so a
tuning change cannot silently ship, and dev runs (25 / 15s) are proven to reach
both the semaphore in src/ingestion/rss.py and fetch_feed(). No network calls.
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

from src.ingestion.rss import ingest_rss_feeds
from src.ingestion.run import apply_env
from src.shared.config import Settings, get_settings


TUNING_VARS = ("RSS_FETCH_CONCURRENCY", "RSS_FETCH_TIMEOUT")


@pytest.fixture
def clean_tuning_env(monkeypatch, tmp_path):
    """Isolate tuning vars and get_settings() cache, and run in an empty cwd.

    apply_env() reads .env/.env.dev relative to cwd and writes to os.environ
    directly, so the whole environment is snapshotted and restored here.
    """
    snapshot = dict(os.environ)
    for var in TUNING_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    get_settings.cache_clear()


def set_tuning(monkeypatch, **values: str) -> None:
    """Set tuning env vars and drop the cached Settings so the new values apply."""
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


class _RecordingModuleProxy:
    """Stand-in for a module that records how one attribute is constructed.

    Used instead of patching e.g. asyncio.Semaphore in place so the real module
    (shared process-wide) is never mutated during a test.
    """

    def __init__(self, real_module, attr_name: str, calls: list):
        self._real_module = real_module
        self._attr_name = attr_name
        self._calls = calls

    def __getattr__(self, name):
        target = getattr(self._real_module, name)
        if name != self._attr_name:
            return target

        def recording(*args, **kwargs):
            self._calls.append((args, kwargs))
            return target(*args, **kwargs)

        return recording


def _sources(count: int) -> dict:
    return {
        "src_a": {
            "name": "Source A",
            "domain": "example.com",
            "tier": "tier1",
            "feeds": [f"https://example.com/feed/{i}" for i in range(count)],
        }
    }


class _EmptyFeed:
    def __init__(self):
        self.entries = []


class TestSettingDefaults:
    """(a) Setting default is 10, and the env override works."""

    def test_default_concurrency_is_10(self, monkeypatch):
        """Production default is 10 concurrent feed fetches."""
        monkeypatch.delenv("RSS_FETCH_CONCURRENCY", raising=False)
        assert Settings().rss_fetch_concurrency == 10

    def test_default_timeout_is_30(self, monkeypatch):
        """Production default per-feed timeout is unchanged at 30s."""
        monkeypatch.delenv("RSS_FETCH_TIMEOUT", raising=False)
        assert Settings().rss_fetch_timeout == 30

    def test_concurrency_env_override(self, monkeypatch):
        """RSS_FETCH_CONCURRENCY overrides the default."""
        monkeypatch.setenv("RSS_FETCH_CONCURRENCY", "25")
        assert Settings().rss_fetch_concurrency == 25

    def test_timeout_env_override(self, monkeypatch):
        """RSS_FETCH_TIMEOUT overrides the default via pydantic-settings."""
        monkeypatch.setenv("RSS_FETCH_TIMEOUT", "15")
        assert Settings().rss_fetch_timeout == 15

    def test_timeout_env_override_is_not_blocked_by_defaults(self, monkeypatch):
        """The override is genuinely read from the environment, not the default."""
        monkeypatch.setenv("RSS_FETCH_TIMEOUT", "7")
        assert Settings().rss_fetch_timeout != 30
        assert Settings().rss_fetch_timeout == 7

    def test_zero_concurrency_rejected(self):
        """Semaphore(0) would deadlock, so zero is a validation error."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(rss_fetch_concurrency=0)
        assert "rss_fetch_concurrency" in str(exc_info.value)


class TestSemaphoreFromSetting:
    """(b) rss.py builds the fetch semaphore from the setting."""

    @pytest.mark.asyncio
    async def test_fetch_semaphore_constructed_from_setting(self, monkeypatch):
        """The first Semaphore built is the fetch one, sized from the setting."""
        set_tuning(monkeypatch, RSS_FETCH_CONCURRENCY="7")
        calls: list = []
        proxy = _RecordingModuleProxy(asyncio, "Semaphore", calls)

        with patch("src.ingestion.rss.asyncio", proxy), \
             patch("src.ingestion.rss.fetch_feed", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = lambda *a, **k: _EmptyFeed()
            await ingest_rss_feeds(sources=_sources(1))

        values = [args[0] for args, _ in calls]
        assert values[0] == 7, f"fetch semaphore should be 7, built {values}"
        assert 10 not in values, f"hardcoded 10 still used: {values}"

    @pytest.mark.asyncio
    async def test_fetch_concurrency_bounds_inflight_fetches(self, monkeypatch):
        """Observed peak in-flight fetches stays at the configured bound."""
        set_tuning(monkeypatch, RSS_FETCH_CONCURRENCY="2")
        in_flight = 0
        peak = 0

        async def slow_fetch(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return _EmptyFeed()

        with patch("src.ingestion.rss.fetch_feed", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = slow_fetch
            await ingest_rss_feeds(sources=_sources(9))

        assert peak <= 2, f"peak in-flight fetches {peak} exceeded concurrency 2"
        assert peak == 2, f"expected the bound to be saturated, peaked at {peak}"

    @pytest.mark.asyncio
    async def test_higher_concurrency_allows_more_in_parallel(self, monkeypatch):
        """Raising the setting raises the observed bound (not a fixed cap)."""
        set_tuning(monkeypatch, RSS_FETCH_CONCURRENCY="6")
        in_flight = 0
        peak = 0

        async def slow_fetch(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return _EmptyFeed()

        with patch("src.ingestion.rss.fetch_feed", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = slow_fetch
            await ingest_rss_feeds(sources=_sources(9))

        assert peak == 6, f"expected peak 6 in-flight fetches, peaked at {peak}"


class TestTimeoutFlowsIntoFetch:
    """(c) The timeout override reaches the HTTP client and every fetch_feed call."""

    @pytest.mark.asyncio
    async def test_timeout_override_reaches_fetch_feed(self, monkeypatch):
        set_tuning(monkeypatch, RSS_FETCH_TIMEOUT="7")
        client_calls: list = []
        proxy = _RecordingModuleProxy(httpx, "AsyncClient", client_calls)

        with patch("src.ingestion.rss.httpx", proxy), \
             patch("src.ingestion.rss.fetch_feed", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = lambda *a, **k: _EmptyFeed()
            await ingest_rss_feeds(sources=_sources(3))

        assert mock_fetch.await_count == 3
        timeouts = {call.kwargs["timeout"] for call in mock_fetch.await_args_list}
        assert timeouts == {7}, f"fetch_feed got timeouts {timeouts}"
        client_kwargs = [kwargs for _, kwargs in client_calls]
        assert all(kw.get("timeout") == 7 for kw in client_kwargs), client_kwargs

    @pytest.mark.asyncio
    async def test_default_timeout_reaches_fetch_feed(self, monkeypatch):
        """Sanity check on the other side: the default 30 still arrives."""
        monkeypatch.delenv("RSS_FETCH_TIMEOUT", raising=False)
        get_settings.cache_clear()

        with patch("src.ingestion.rss.fetch_feed", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = lambda *a, **k: _EmptyFeed()
            await ingest_rss_feeds(sources=_sources(1))

        assert mock_fetch.await_args_list[0].kwargs["timeout"] == 30


class TestApplyEnvProfiles:
    """Dev runs get 25 / 15; prod is untouched."""

    def test_dev_applies_tuning_defaults(self, clean_tuning_env, capsys):
        apply_env("dev")
        assert os.environ["RSS_FETCH_CONCURRENCY"] == "25"
        assert os.environ["RSS_FETCH_TIMEOUT"] == "15"
        settings = get_settings()
        assert settings.rss_fetch_concurrency == 25
        assert settings.rss_fetch_timeout == 15
        assert "RSS_FETCH_CONCURRENCY=25" in capsys.readouterr().out

    def test_prod_leaves_production_defaults(self, clean_tuning_env):
        apply_env("prod")
        settings = get_settings()
        assert settings.rss_fetch_concurrency == 10
        assert settings.rss_fetch_timeout == 30

    def test_dev_respects_explicit_process_env(self, clean_tuning_env, monkeypatch):
        monkeypatch.setenv("RSS_FETCH_CONCURRENCY", "3")
        monkeypatch.setenv("RSS_FETCH_TIMEOUT", "5")
        apply_env("dev")
        assert get_settings().rss_fetch_concurrency == 3
        assert get_settings().rss_fetch_timeout == 5

    def test_dev_env_dev_file_overrides_default(self, clean_tuning_env, tmp_path):
        """A value in .env.dev beats the dev default."""
        (tmp_path / ".env.dev").write_text("RSS_FETCH_CONCURRENCY=7\nRSS_FETCH_TIMEOUT=9\n")
        apply_env("dev")
        assert get_settings().rss_fetch_concurrency == 7
        assert get_settings().rss_fetch_timeout == 9

    def test_env_file_overrides_dev_default(self, clean_tuning_env, tmp_path):
        """A value in .env also beats the dev default."""
        (tmp_path / ".env").write_text("RSS_FETCH_TIMEOUT=11\n")
        apply_env("dev")
        assert get_settings().rss_fetch_timeout == 11
        assert get_settings().rss_fetch_concurrency == 25


if __name__ == "__main__":
    pytest.main([__file__, "-v"])