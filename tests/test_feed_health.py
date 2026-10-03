"""Tests for the dead-feed monitor (src/ingestion/feed_health.py).

The behaviour under test is a promise about reporting: a feed that keeps
returning nothing is eventually called dead, said out loud, and called back to
life when it yields entries again. The tests use a fixed clock and an explicit
registry so nothing here depends on wall time or on the process-wide state.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.ingestion import feed_health
from src.ingestion.feed_health import (
    DEAD_AFTER_EMPTY_POLLS,
    STATUS_DEAD,
    STATUS_EMPTY,
    STATUS_OK,
    HealthRegistry,
    load_registry,
    record_poll,
    render_health_report,
    report_dead_feeds,
    save_registry,
    state_path,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class TestRecordPoll:
    def test_entries_count_as_a_yield(self):
        reg = HealthRegistry()
        feed = record_poll(reg, "https://x.test/feed", 20, source_key="x.test", now=_at(0))
        assert feed.status == STATUS_OK
        assert feed.entries_total == 20
        assert feed.last_entries == 20
        assert feed.last_poll_at == T0.isoformat()
        assert feed.last_yield_at == T0.isoformat()
        assert feed.source_key == "x.test"

    def test_stays_ok_on_repeated_polls(self):
        reg = HealthRegistry()
        for i in range(10):
            record_poll(reg, "https://x.test/feed", 5, now=_at(i))
        assert reg.get("https://x.test/feed").status == STATUS_OK
        assert reg.dead() == []

    def test_one_empty_poll_is_empty_not_dead(self):
        reg = HealthRegistry()
        record_poll(reg, "https://x.test/feed", 5, now=_at(0))
        feed = record_poll(reg, "https://x.test/feed", 0, detail="http_404", now=_at(10))
        assert feed.status == STATUS_EMPTY
        assert feed.consecutive_empty == 1
        assert feed.empty_polls == 1
        assert feed.last_detail == "http_404"
        assert reg.dead() == []

    def test_dead_after_consecutive_empty_threshold(self):
        reg = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            feed = record_poll(reg, "https://x.test/feed", 0, detail="http_404", now=_at(i))
        assert feed.status == STATUS_DEAD
        assert feed.dead_since == _at(DEAD_AFTER_EMPTY_POLLS - 1).isoformat()
        assert [f.url for f in reg.dead()] == ["https://x.test/feed"]

    def test_non_consecutive_empties_never_reach_dead(self):
        reg = HealthRegistry()
        for i in range(9):
            record_poll(reg, "https://x.test/feed", 0, detail="http_500", now=_at(i))
            record_poll(reg, "https://x.test/feed", 1, now=_at(i))
        feed = reg.get("https://x.test/feed")
        assert feed.empty_polls == 9
        assert feed.polls == 18
        assert feed.status == STATUS_OK
        assert reg.dead() == []

    def test_counts_entries_not_ingested_articles(self):
        """The scmp case: 50 entries, every article page 403, zero articles.

        If the monitor counted ingested articles it would call this feed
        healthy, which is exactly the blind spot it exists to close.
        """
        reg = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            feed = record_poll(reg, "https://scmp.test/rss", 50, now=_at(i))
        assert feed.status == STATUS_OK
        assert feed.entries_total == 150

    def test_recovery_clears_dead_and_is_counted(self):
        reg = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(reg, "https://x.test/feed", 0, detail="timeout", now=_at(i))
        assert reg.get("https://x.test/feed").status == STATUS_DEAD
        feed = record_poll(reg, "https://x.test/feed", 12, now=_at(60))
        assert feed.status == STATUS_OK
        assert feed.consecutive_empty == 0
        assert feed.dead_since is None
        assert feed.recovered_count == 1
        assert reg.dead() == []

    def test_recovery_twice_counts_twice(self):
        reg = HealthRegistry()
        for _ in range(2):
            for i in range(DEAD_AFTER_EMPTY_POLLS):
                record_poll(reg, "https://x.test/feed", 0, now=_at(i))
            record_poll(reg, "https://x.test/feed", 3, now=_at(99))
        assert reg.get("https://x.test/feed").recovered_count == 2

    def test_feeds_are_tracked_independently(self):
        reg = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(reg, "https://dead.test/feed", 0, now=_at(i))
        record_poll(reg, "https://live.test/feed", 9, now=_at(3))
        assert [f.url for f in reg.dead()] == ["https://dead.test/feed"]
        assert reg.get("https://live.test/feed").status == STATUS_OK

    def test_source_key_only_set_when_given(self):
        reg = HealthRegistry()
        record_poll(reg, "https://x.test/feed", 1, source_key="first", now=_at(0))
        record_poll(reg, "https://x.test/feed", 1, now=_at(1))
        assert reg.get("https://x.test/feed").source_key == "first"

    def test_no_clock_argument_defaults_to_now(self):
        reg = HealthRegistry()
        before = datetime.now(timezone.utc)
        feed = record_poll(reg, "https://x.test/feed", 1)
        assert datetime.fromisoformat(feed.last_poll_at) >= before


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "health.json")
        reg = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(reg, "https://x.test/feed", 0, source_key="x.test",
                        detail="http_404", now=_at(i))
        assert save_registry(reg, path) is True
        loaded = load_registry(path)
        feed = loaded.get("https://x.test/feed")
        assert feed.status == STATUS_DEAD
        assert feed.consecutive_empty == DEAD_AFTER_EMPTY_POLLS
        assert feed.dead_since == _at(DEAD_AFTER_EMPTY_POLLS - 1).isoformat()
        assert loaded.dead()[0].source_key == "x.test"

    def test_streak_survives_a_restart(self, tmp_path):
        """Two runs, two processes' worth of state: the counter must not reset."""
        path = str(tmp_path / "health.json")
        reg = HealthRegistry()
        record_poll(reg, "https://x.test/feed", 0, now=_at(0))
        save_registry(reg, path)
        reg2 = load_registry(path)
        record_poll(reg2, "https://x.test/feed", 0, now=_at(10))
        save_registry(reg2, path)
        reg3 = load_registry(path)
        record_poll(reg3, "https://x.test/feed", 0, now=_at(20))
        assert reg3.get("https://x.test/feed").status == STATUS_DEAD

    def test_missing_file_is_empty_registry(self, tmp_path):
        assert load_registry(str(tmp_path / "nope.json")).feeds == {}

    def test_corrupt_file_is_empty_registry_not_an_error(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_registry(str(path)).feeds == {}

    def test_wrong_shape_is_ignored(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        assert load_registry(str(path)).feeds == {}

    def test_malformed_entry_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(
            json.dumps({"https://x.test/feed": {"nonsense": 1},
                        "https://y.test/feed": {"url": "https://y.test/feed", "polls": 2}}),
            encoding="utf-8",
        )
        loaded = load_registry(str(path))
        assert loaded.get("https://x.test/feed").polls == 0
        assert loaded.get("https://y.test/feed").polls == 2

    def test_save_failure_returns_false(self, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.write_text("i am a file, not a directory", encoding="utf-8")
        assert save_registry(HealthRegistry(), str(blocked / "sub" / "health.json")) is False

    def test_unknown_fields_are_ignored_on_load(self, tmp_path):
        path = tmp_path / "health.json"
        path.write_text(
            json.dumps({"https://x.test/feed": {"url": "https://x.test/feed",
                                               "polls": 1, "future_field": "boom"}}),
            encoding="utf-8",
        )
        assert load_registry(str(path)).get("https://x.test/feed").polls == 1

    def test_state_path_prefers_argument_then_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEED_HEALTH_STATE_PATH", str(tmp_path / "from-env.json"))
        assert state_path(str(tmp_path / "explicit.json")) == str(tmp_path / "explicit.json")
        assert state_path(None) == str(tmp_path / "from-env.json")
        monkeypatch.delenv("FEED_HEALTH_STATE_PATH")
        assert state_path(None) == feed_health.DEFAULT_STATE_PATH


class TestReporting:
    def test_report_dead_feeds_shape(self):
        reg = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(reg, "https://x.test/feed", 0, source_key="x.test",
                        detail="http_404", now=_at(i))
        dead = report_dead_feeds(reg)
        assert len(dead) == 1
        row = dead[0]
        assert row["url"] == "https://x.test/feed"
        assert row["source_key"] == "x.test"
        assert row["consecutive_empty"] == DEAD_AFTER_EMPTY_POLLS
        assert row["last_detail"] == "http_404"
        assert row["dead_since"] == _at(DEAD_AFTER_EMPTY_POLLS - 1).isoformat()
        assert json.dumps(dead)  # must be serializable into the run results

    def test_dead_feeds_sorted_by_url(self):
        reg = HealthRegistry()
        for url in ("https://z.test/f", "https://a.test/f", "https://m.test/f"):
            for i in range(DEAD_AFTER_EMPTY_POLLS):
                record_poll(reg, url, 0, now=_at(i))
        assert [r["url"] for r in report_dead_feeds(reg)] == [
            "https://a.test/f", "https://m.test/f", "https://z.test/f"
        ]

    def test_markdown_puts_dead_first_and_names_every_feed(self):
        reg = HealthRegistry()
        record_poll(reg, "https://live.test/feed", 4, source_key="live", now=_at(0))
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(reg, "https://rot.test/feed", 0, source_key="rot",
                        detail="timeout", now=_at(i))
        md = render_health_report(reg)
        lines = md.strip().splitlines()
        assert lines[0].startswith("| feed |")
        assert "https://rot.test/feed" in lines[2]
        assert STATUS_DEAD in lines[2]
        assert "https://live.test/feed" in lines[3]
        assert STATUS_OK in lines[3]

    def test_markdown_of_empty_registry_says_so(self):
        assert render_health_report(HealthRegistry()) == "_No feed health recorded yet._"


class TestActiveRegistry:
    def test_load_active_and_save_active(self, tmp_path, monkeypatch):
        path = str(tmp_path / "active.json")
        monkeypatch.setattr(feed_health, "_ACTIVE", HealthRegistry())
        record_poll(feed_health.active(), "https://x.test/feed", 0, now=_at(0))
        assert feed_health.save_active(path) is True
        monkeypatch.setattr(feed_health, "_ACTIVE", HealthRegistry())
        reloaded = feed_health.load_active(path)
        assert reloaded.get("https://x.test/feed").polls == 1
        assert feed_health.active() is reloaded


class TestRssWiring:
    """The monitor is only as good as its hook: fetch_feed must record every
    terminal outcome, including the ones that used to be silent."""

    @pytest.mark.asyncio
    async def test_successful_fetch_records_entry_count(self, monkeypatch):
        import feedparser
        from src.ingestion import rss

        registry = HealthRegistry()
        monkeypatch.setattr(feed_health, "_ACTIVE", registry)
        monkeypatch.setattr(rss, "get_settings", lambda: type("S", (), {
            "rss_max_retries": 1, "rss_retry_delay": 0,
        })())

        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            "<item><title>a</title><link>https://x.test/a</link></item>"
            "<item><title>b</title><link>https://x.test/b</link></item>"
            "</channel></rss>"
        )

        class FakeResponse:
            text = xml

            def raise_for_status(self):
                return None

        class FakeClient:
            async def get(self, url, timeout=None, follow_redirects=False):
                return FakeResponse()

        feed = await rss.fetch_feed(FakeClient(), "https://x.test/feed", source_key="x.test")
        assert feed is not None
        health = registry.get("https://x.test/feed")
        assert health.status == STATUS_OK
        assert health.entries_total == 2
        assert health.last_detail == "ok"
        assert health.source_key == "x.test"
        assert feedparser  # imported symbol used

    @pytest.mark.asyncio
    async def test_empty_but_valid_feed_records_zero_entries(self, monkeypatch):
        from src.ingestion import rss

        registry = HealthRegistry()
        monkeypatch.setattr(feed_health, "_ACTIVE", registry)
        monkeypatch.setattr(rss, "get_settings", lambda: type("S", (), {
            "rss_max_retries": 1, "rss_retry_delay": 0,
        })())

        xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'

        class FakeResponse:
            text = xml

            def raise_for_status(self):
                return None

        class FakeClient:
            async def get(self, url, timeout=None, follow_redirects=False):
                return FakeResponse()

        feed = await rss.fetch_feed(FakeClient(), "https://x.test/feed", source_key="x.test")
        assert feed is not None
        health = registry.get("https://x.test/feed")
        assert health.status == STATUS_EMPTY
        assert health.last_detail == "0_entries"

    @pytest.mark.asyncio
    async def test_404_records_detail_and_never_retries(self, monkeypatch):
        import httpx
        from src.ingestion import rss

        registry = HealthRegistry()
        monkeypatch.setattr(feed_health, "_ACTIVE", registry)
        monkeypatch.setattr(rss, "get_settings", lambda: type("S", (), {
            "rss_max_retries": 3, "rss_retry_delay": 0,
        })())

        calls = []

        class FakeClient:
            async def get(self, url, timeout=None, follow_redirects=False):
                calls.append(url)
                request = httpx.Request("GET", url)
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("404", request=request, response=response)

        feed = await rss.fetch_feed(FakeClient(), "https://x.test/feed", source_key="x.test")
        assert feed is None
        assert len(calls) == 1, "4xx must not be retried"
        health = registry.get("https://x.test/feed")
        assert health.status == STATUS_EMPTY
        assert health.last_detail == "http_404"

    @pytest.mark.asyncio
    async def test_not_a_feed_records_zero(self, monkeypatch):
        from src.ingestion import rss

        registry = HealthRegistry()
        monkeypatch.setattr(feed_health, "_ACTIVE", registry)
        monkeypatch.setattr(rss, "get_settings", lambda: type("S", (), {
            "rss_max_retries": 1, "rss_retry_delay": 0,
        })())

        class FakeResponse:
            text = "<html><body>Just a moment...</body></html>"

            def raise_for_status(self):
                return None

        class FakeClient:
            async def get(self, url, timeout=None, follow_redirects=False):
                return FakeResponse()

        feed = await rss.fetch_feed(FakeClient(), "https://x.test/feed", source_key="x.test")
        assert feed is None
        health = registry.get("https://x.test/feed")
        assert health.status == STATUS_EMPTY
        assert health.last_detail == "not_a_feed"

    @pytest.mark.asyncio
    async def test_repeated_failures_reach_dead(self, monkeypatch):
        from src.ingestion import rss

        registry = HealthRegistry()
        monkeypatch.setattr(feed_health, "_ACTIVE", registry)
        monkeypatch.setattr(rss, "get_settings", lambda: type("S", (), {
            "rss_max_retries": 1, "rss_retry_delay": 0,
        })())

        class FakeResponse:
            text = "<html>nope</html>"

            def raise_for_status(self):
                return None

        class FakeClient:
            async def get(self, url, timeout=None, follow_redirects=False):
                return FakeResponse()

        for _ in range(DEAD_AFTER_EMPTY_POLLS):
            await rss.fetch_feed(FakeClient(), "https://x.test/feed", source_key="x.test")
        assert [f.url for f in registry.dead()] == ["https://x.test/feed"]


class TestRunReporting:
    """The run must say it, not just record it: the dead verdict has to reach
    the results the pipeline logs and the console the operator reads."""

    @pytest.mark.asyncio
    async def test_run_reports_dead_feeds_in_results(self, tmp_path, monkeypatch, capsys):
        from src.ingestion import run as run_mod

        state = tmp_path / "health.json"
        monkeypatch.setenv("FEED_HEALTH_STATE_PATH", str(state))
        registry = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(registry, "https://rot.test/feed", 0, source_key="rot.test",
                        detail="http_404", now=_at(i))
        save_registry(registry, str(state))

        mock_session = AsyncMock()
        with patch("src.ingestion.run.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = mock_session
            results = await run_mod.run_ingestion(dry_run=True, tiers=[])

        feed_health_block = results["phases"]["ingestion"]["feed_health"]
        assert feed_health_block["state_saved"] is True
        assert [d["url"] for d in feed_health_block["dead_feeds"]] == ["https://rot.test/feed"]
        assert feed_health_block["dead_feeds"][0]["last_detail"] == "http_404"
        assert "DEAD" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_run_reports_no_dead_feeds(self, tmp_path, monkeypatch, capsys):
        from src.ingestion import run as run_mod

        monkeypatch.setenv("FEED_HEALTH_STATE_PATH", str(tmp_path / "health.json"))
        mock_session = AsyncMock()
        with patch("src.ingestion.run.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = mock_session
            results = await run_mod.run_ingestion(dry_run=True, tiers=[])

        assert results["phases"]["ingestion"]["feed_health"]["dead_feeds"] == []
        assert "no dead feeds" in capsys.readouterr().out


class TestReportScript:
    """The standalone script is the part an operator runs on demand."""

    def test_exit_1_and_table_when_dead(self, tmp_path, monkeypatch, capsys):
        from scripts.report_feed_health import main

        state = tmp_path / "health.json"
        registry = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(registry, "https://rot.test/feed", 0, source_key="rot.test",
                        detail="timeout", now=_at(i))
        save_registry(registry, str(state))

        assert main(["--state-path", str(state)]) == 1
        out = capsys.readouterr().out
        assert "https://rot.test/feed" in out
        assert "DEAD" in out

    def test_exit_0_when_healthy(self, tmp_path, capsys):
        from scripts.report_feed_health import main

        state = tmp_path / "health.json"
        registry = HealthRegistry()
        record_poll(registry, "https://live.test/feed", 7, source_key="live.test", now=_at(0))
        save_registry(registry, str(state))

        assert main(["--state-path", str(state)]) == 0
        assert "No dead feeds" in capsys.readouterr().out

    def test_json_output_is_parseable(self, tmp_path, capsys):
        import json as json_mod

        from scripts.report_feed_health import main

        state = tmp_path / "health.json"
        registry = HealthRegistry()
        for i in range(DEAD_AFTER_EMPTY_POLLS):
            record_poll(registry, "https://rot.test/feed", 0, now=_at(i))
        save_registry(registry, str(state))

        assert main(["--state-path", str(state), "--json"]) == 1
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload[0]["url"] == "https://rot.test/feed"

    def test_missing_state_file_is_not_a_crash(self, tmp_path):
        from scripts.report_feed_health import main

        assert main(["--state-path", str(tmp_path / "absent.json")]) == 0


class TestRegistryHonesty:
    def test_no_enabled_source_without_an_ingestion_path(self):
        """Every enabled source must have either feeds or a known adapter.

        bsky.social and substack.com were both enabled with no feed URLs and no
        adapter, which is how the registry claimed coverage that did not exist.
        """
        from src.ingestion.source_registry import ALL_SOURCES

        adapted_without_feeds = {
            "reddit.com",  # reddit.py
            "earthquake.usgs.gov",  # sensors.py
            "gdacs.org",  # sensors.py
        }
        offenders = {
            domain
            for domain, cfg in ALL_SOURCES.items()
            if cfg.enabled and not cfg.rss_urls and domain not in adapted_without_feeds
        }
        assert offenders == set(), f"enabled with no adapter and no feeds: {sorted(offenders)}"

    def test_bsky_and_substack_are_disabled_with_a_reason(self):
        from src.ingestion.source_registry import ALL_SOURCES

        for domain in ("bsky.social", "substack.com"):
            cfg = ALL_SOURCES[domain]
            assert cfg.enabled is False
            assert cfg.notes, f"{domain} must say why it is disabled"
