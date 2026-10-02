"""Tests for the P0 ingestion-robustness fixes (I-P1-1, I-P1-3, I-P1-4, I-P1-7).

- I-P1-1: a healthy filtered run (no tier-1 RSS adapters ran) must not exit 1.
- I-P1-3: one adapter raising must not destroy the whole run.
- I-P1-7: a corrupt GKG zip degrades its window instead of ending the run.
- I-P1-4: feed_ok is recorded only after the payload parses as a feed.
"""

import csv
import sys
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.ingestion.adapter import SourceHealth
from src.ingestion.run import main, run_ingestion
from src.schema.models import RawArticle, SourceTier
from src.utils.ingest_stats import STATS
from src.utils.trafilatura_extract import compute_content_hash, compute_url_hash


# ---------------------------------------------------------------------------
# I-P1-1: filtered run must not exit 1


def _filtered_gdelt_stats():
    """Stats snapshot from a --sources gdelt run: no tier-1 domain keys."""
    return {
        "gdelt_static.20261002120000.entries_in_feed": 4,
        "gdelt_static.20261002120000.entries_seen": 120,
        "gdelt_static.20261002120000.ok": 3,
    }


class TestFilteredRunExitCode:
    """I-P1-1: a healthy filtered run is not marked FAILED."""

    @pytest.mark.asyncio
    async def test_filtered_run_no_tier1_keys_no_exit(self):
        """--sources gdelt with articles fetched exits 0 (no SystemExit)."""
        with patch("src.ingestion.run.run_ingestion", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "total_fetched": 500,
                        "total_new": 12,
                        "extraction_stats": _filtered_gdelt_stats(),
                        "tier1_critical_down": [],
                        "gdelt_health": {"failed": [], "succeeded": ["bbc.com"], "skipped": []},
                    }
                }
            }
            with patch.object(sys, "argv", ["run.py", "--sources", "gdelt"]):
                # Must not raise SystemExit: tier-1 RSS never ran, so the
                # tier-1 health guard has nothing to judge.
                await main()

    @pytest.mark.asyncio
    async def test_filtered_run_zero_fetched_still_exits(self):
        """The total_fetched==0 guard is independent of the tier-1 guard."""
        with patch("src.ingestion.run.run_ingestion", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "total_fetched": 0,
                        "extraction_stats": _filtered_gdelt_stats(),
                        "tier1_critical_down": [],
                    }
                }
            }
            with patch.object(sys, "argv", ["run.py", "--sources", "gdelt"]):
                with pytest.raises(SystemExit) as exc_info:
                    await main()
                assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_broken_tier1_source_still_exits_1(self):
        """Control: when tier-1 RSS ran and is broken, exit 1 still fires."""
        stats = {
            "bbc.com.feed_ok": 0,
            "bbc.com.feed_failed": 3,
            "bbc.com.entries_in_feed": 0,
            "bbc.com.ok": 0,
            "bbc.com.already_known": 0,
        }
        with patch("src.ingestion.run.run_ingestion", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "total_fetched": 10,
                        "extraction_stats": stats,
                        "tier1_critical_down": [],
                        "gdelt_health": {"failed": [], "succeeded": [], "skipped": []},
                    }
                }
            }
            with patch.object(sys, "argv", ["run.py"]):
                with pytest.raises(SystemExit) as exc_info:
                    await main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# I-P1-3: adapter raise containment


class _BoomAdapter:
    """Adapter whose fetch() raises, as the contract permits."""

    name = "boom"

    async def fetch(self):
        raise RuntimeError("total adapter failure")

    async def health_check(self):
        return SourceHealth(status="ok")


class _GoodAdapter:
    name = "good"

    def __init__(self, articles):
        self._articles = articles

    async def fetch(self):
        return list(self._articles)

    async def health_check(self):
        return SourceHealth(status="ok", detail="fine")


def _make_article():
    url = "https://example.com/article/good-1"
    return RawArticle(
        id=uuid.uuid4(),
        url=url,
        url_hash=compute_url_hash(url),
        title="Good article",
        body_text="Body text of a perfectly fine article.",
        source_domain="example.com",
        source_tier=SourceTier.TIER3,
        content_hash=compute_content_hash("Body text of a perfectly fine article."),
    )


class TestAdapterRaiseContainment:
    """I-P1-3: one adapter raising does not destroy the run."""

    @pytest.mark.asyncio
    async def test_raising_adapter_contained(self):
        article = _make_article()

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None
        url_hash_result = MagicMock()
        url_hash_result.scalars.return_value.all.return_value = []
        content_hash_result = MagicMock()
        content_hash_result.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[url_hash_result, content_hash_result])
        mock_session.commit = AsyncMock()

        with patch(
            "src.ingestion.run.build_adapters",
            return_value=[_BoomAdapter(), _GoodAdapter([article])],
        ), patch("src.ingestion.run.get_session", return_value=mock_session), patch(
            "src.ingestion.run.log_status"
        ), patch(
            "src.enrichment.translation.translate_articles",
            return_value={"backend": "test", "translated": 0, "english": 1, "failed": 0, "total": 1},
        ):
            STATS.reset()
            results = await run_ingestion(dry_run=True)

        ingestion = results["phases"]["ingestion"]
        # The good adapter's article survived the bad adapter's raise.
        assert ingestion["total_fetched"] == 1
        assert ingestion["total_new"] == 1
        # The failure is recorded in adapter_health, not fatal.
        health = ingestion["adapter_health"]
        assert health["boom"]["status"] == "down"
        assert "RuntimeError" in health["boom"]["detail"]
        assert health["good"]["status"] == "ok"
        # And it is visible in the stats ledger.
        snapshot = STATS.snapshot()
        assert snapshot.get("adapter.boom.fetch_raised", 0) == 1


# ---------------------------------------------------------------------------
# I-P1-7: bad GKG window degrades instead of exploding


class TestBadGkgWindow:
    """I-P1-7: a corrupt GKG zip degrades its window."""

    def _window(self, monkeypatch, tmp_path, download_bytes: bytes):
        from src.ingestion import gdelt_static

        async def fake_download(url, dest, max_bytes):
            with open(dest, "wb") as f:
                f.write(download_bytes)
            return True

        monkeypatch.setattr(gdelt_static, "_download", fake_download)
        STATS.reset()
        result = gdelt_static.StaticResult()
        return gdelt_static, result

    @pytest.mark.asyncio
    async def test_garbage_zip_degrades_window(self, monkeypatch, tmp_path):
        """Non-zip bytes (BadZipFile) -> soft skip, window ok, no raise."""
        gdelt_static, result = self._window(monkeypatch, tmp_path, b"this is not a zip file")
        stamp = "20261002120000"
        await gdelt_static._ingest_one_window(
            stamp, str(tmp_path), result, 500, 10_000_000, set(), False, True
        )
        assert result.ok is True
        assert result.error is None
        assert f"gkg parse {stamp}" in result.soft_skips
        assert result.articles == []
        assert STATS.snapshot().get(f"gdelt_static.{stamp}.gkg_parse_failed", 0) == 1

    @pytest.mark.asyncio
    async def test_csv_error_degrades_window(self, monkeypatch, tmp_path):
        """csv.Error from the GKG parser -> soft skip, no raise."""
        gdelt_static, result = self._window(monkeypatch, tmp_path, b"unused")

        def boom(*args, **kwargs):
            raise csv.Error("synthetic malformed row")

        monkeypatch.setattr(gdelt_static, "parse_gkg_file", boom)
        stamp = "20261002121500"
        await gdelt_static._ingest_one_window(
            stamp, str(tmp_path), result, 500, 10_000_000, set(), False, True
        )
        assert result.ok is True
        assert f"gkg parse {stamp}" in result.soft_skips

    @pytest.mark.asyncio
    async def test_valid_zip_still_parses(self, tmp_path):
        """Control: a well-formed (empty) GKG zip does not trip the guard."""
        from src.ingestion import gdelt_static

        zpath = tmp_path / "20261002120000.gkg.csv.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("20261002120000.gkg.csv", "")
        articles, rows, joined, skipped = gdelt_static.parse_gkg_file(
            str(zpath), {}, "gdelt-static", 500, set()
        )
        assert articles == [] and rows == 0


# ---------------------------------------------------------------------------
# I-P1-4: feed_ok recorded only after the payload parses as a feed


def _client_for(body: bytes, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_VALID_RSS = (
    b'<?xml version="1.0"?>'
    b'<rss version="2.0"><channel><title>T</title>'
    b'<item><title>A</title><link>http://example.com/1</link></item>'
    b"</channel></rss>"
)
# Malformed XML that feedparser still recovers entries from (bozo=1).
_BOZO_RSS = (
    b'<rss version="2.0"><channel><title>T</title>'
    b'<item><title>A</title><link>http://example.com/1</link></item>'
    b"</channel>"
)
# A 200 Cloudflare-style interstitial: 2xx but not a feed.
_INTERSTITIAL = (
    b"<html><head><title>Just a moment...</title></head>"
    b"<body>Verifying you are human</body></html>"
)


class TestFeedOkAfterParse:
    """I-P1-4: feed_ok requires a parseable feed payload."""

    @pytest.mark.asyncio
    async def test_valid_feed_records_feed_ok(self):
        from src.ingestion import rss as rss_mod

        STATS.reset()
        async with _client_for(_VALID_RSS) as client:
            feed = await rss_mod.fetch_feed(client, "http://example.com/rss", source_key="example.com")
        assert feed is not None
        assert len(feed.entries) == 1
        snapshot = STATS.snapshot()
        assert snapshot.get("example.com.feed_ok", 0) == 1
        assert "example.com.feed_failed:not_a_feed" not in snapshot

    @pytest.mark.asyncio
    async def test_interstitial_not_recorded_feed_ok(self):
        """A 200 HTML interstitial is feed_failed, never feed_ok."""
        from src.ingestion import rss as rss_mod

        STATS.reset()
        async with _client_for(_INTERSTITIAL) as client:
            feed = await rss_mod.fetch_feed(client, "http://example.com/rss", source_key="example.com")
        assert feed is None
        snapshot = STATS.snapshot()
        assert snapshot.get("example.com.feed_ok", 0) == 0
        assert snapshot.get("example.com.feed_failed:not_a_feed", 0) == 1

    @pytest.mark.asyncio
    async def test_bozo_with_entries_still_ok_but_flagged(self):
        """Malformed-but-recoverable feeds stay feed_ok; the wobble is flagged."""
        from src.ingestion import rss as rss_mod

        STATS.reset()
        async with _client_for(_BOZO_RSS) as client:
            feed = await rss_mod.fetch_feed(client, "http://example.com/rss", source_key="example.com")
        assert feed is not None
        assert len(feed.entries) == 1
        snapshot = STATS.snapshot()
        assert snapshot.get("example.com.feed_ok", 0) == 1
        assert snapshot.get("example.com.feed_bozo", 0) == 1

    @pytest.mark.asyncio
    async def test_empty_body_not_recorded_feed_ok(self):
        """A 200 with an empty body is not a feed either."""
        from src.ingestion import rss as rss_mod

        STATS.reset()
        async with _client_for(b"") as client:
            feed = await rss_mod.fetch_feed(client, "http://example.com/rss", source_key="example.com")
        assert feed is None
        snapshot = STATS.snapshot()
        assert snapshot.get("example.com.feed_ok", 0) == 0
