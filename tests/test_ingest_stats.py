"""Tests for IngestStats."""

from src.utils.ingest_stats import IngestStats


class TestIngestStats:
    """Test the IngestStats class."""

    def test_reset_clears_counts(self):
        stats = IngestStats()
        stats.record("rss", "ok", 5)
        stats.reset()
        assert stats.snapshot() == {}

    def test_record_accumulates(self):
        stats = IngestStats()
        stats.record("rss", "ok", 3)
        stats.record("rss", "ok", 2)
        stats.record("gdelt", "failed", 1)
        snap = stats.snapshot()
        assert snap["rss.ok"] == 5
        assert snap["gdelt.failed"] == 1

    def test_snapshot_returns_flat_dict(self):
        stats = IngestStats()
        stats.record("rss", "ok", 2)
        snap = stats.snapshot()
        assert isinstance(snap, dict)
        assert "rss.ok" in snap

    def test_render_markdown_empty(self):
        stats = IngestStats()
        md = stats.render_markdown()
        assert "No ingestion stats recorded" in md

    def test_render_markdown_table(self):
        stats = IngestStats()
        stats.record("rss", "ok", 5)
        stats.record("rss", "too_short", 1)
        stats.record("gdelt", "ok", 3)
        md = stats.render_markdown()
        assert "| Source | Event | Count |" in md
        assert "| rss | ok | 5 |" in md
        assert "| rss | too_short | 1 |" in md
        assert "| gdelt | ok | 3 |" in md

    def test_thread_safety(self):
        """Basic sanity - multiple records don't corrupt."""
        stats = IngestStats()
        for i in range(100):
            stats.record("rss", "ok")
        assert stats.snapshot()["rss.ok"] == 100