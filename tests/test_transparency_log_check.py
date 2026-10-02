"""Tests for scripts/check_transparency_log.py.

The script exists because the stamping failure was silent: the scheduled
pipeline never called the stamping path, the log stopped growing, and every
/proof page said "pending" with nothing to indicate a fault. So the report
itself has to be trustworthy, and these tests pin the two ways it could lie:
reporting a state it could not read as fine, and building the engine in a way
that dies on the URL form the workflow actually passes.
"""

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import check_transparency_log as check_module
from scripts.check_transparency_log import (
    SCHEDULED_STAMP_CRON,
    check,
    render_report,
    staleness_warning,
)
from src.schema.models import Base, SourceTier
from src.transparency.log import TransparencyBase, canonical_json

NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "transparency-stamp.yml"
)


def _article_row(url, log_index=None):
    """A plain dict of the columns RawArticle needs, for a bulk insert."""
    return {
        "id": uuid.uuid4(),
        "url": url,
        "url_hash": hashlib.sha256(url.encode()).hexdigest(),
        "title": "headline",
        "body_text": "body text long enough to count as stampable " * 5,
        "source_domain": "example.test",
        "source_tier": SourceTier.TIER1.value,
        "published_at": NOW,
        "fetched_at": NOW,
        "content_hash": "ab" * 32,
        "log_index": log_index,
    }


def _entry_row(index, timestamp):
    payload = {"type": "observation", "index": index}
    canonical = canonical_json(payload)
    return {
        "index": index,
        "timestamp": timestamp,
        "payload": payload,
        "canonical_payload": canonical,
        "leaf_hash": hashlib.sha256(canonical).hexdigest(),
        "chain_hash": "cd" * 32,
    }


# --- pure report rendering ------------------------------------------------


def test_render_report_counts_and_share():
    out = render_report(
        log_entries=14,
        stamped_articles=9,
        total_articles=2602,
        last_entry_at=NOW,
    )
    assert "Merkle log entries:      14" in out
    assert "Stamped articles:        9" in out
    assert "Total archived articles: 2602" in out
    assert "Stamped share of archive: 0.35%" in out
    assert NOW.isoformat() in out


def test_render_report_warns_on_an_empty_log():
    out = render_report(
        log_entries=0, stamped_articles=0, total_articles=0, last_entry_at=None
    )
    assert "Newest log entry:        none" in out
    assert "WARNING: the log is empty" in out
    # No percentage line when there is no archive to take a share of.
    assert "Stamped share of archive" not in out


def test_render_report_does_not_gate_on_emptiness():
    """An empty log on a fresh database is correct; the signer refuses it, we report it."""
    out = render_report(
        log_entries=0, stamped_articles=0, total_articles=0, last_entry_at=None
    )
    assert "FAIL" not in out


# --- staleness signal -----------------------------------------------------


def test_staleness_warning_silent_for_a_fresh_entry():
    assert staleness_warning(NOW, now=NOW + timedelta(hours=1)) is None


def test_staleness_warning_names_the_schedule_it_checks():
    out = staleness_warning(NOW, now=NOW + timedelta(days=3))
    assert out is not None
    assert "newest log entry is 72.0h old" in out
    assert SCHEDULED_STAMP_CRON in out


def test_staleness_warning_silent_without_any_entry():
    assert staleness_warning(None) is None


def test_staleness_warning_survives_a_naive_timestamp():
    """SQLite returns naive datetimes for a timezone-aware column; Postgres does not.

    Without the coercion this raises TypeError instead of printing a number.
    """
    naive = NOW.replace(tzinfo=None)
    assert check_module._as_utc(naive) == NOW
    out = staleness_warning(check_module._as_utc(naive), now=NOW + timedelta(days=3))
    assert "72.0h old" in out


def test_staleness_threshold_is_wider_than_the_daily_cadence():
    """48h tolerates one missed run (including a weekend) but not two."""
    assert check_module.STALE_HOURS > 24
    assert check_module.STALE_HOURS < 72


def test_reported_schedule_matches_the_workflow():
    """The report tells the reader which schedule to go look at. Keep them in step."""
    text = WORKFLOW.read_text()
    assert re.search(r"cron:\s*'47 4 \* \* \*'", text), "workflow cron moved"
    assert SCHEDULED_STAMP_CRON == "04:47 UTC"


# --- engine construction --------------------------------------------------


def test_check_requires_a_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert asyncio.run(check()) == 1
    assert "DATABASE_URL not found" in capsys.readouterr().out


def test_check_translates_a_plain_postgres_url(monkeypatch):
    """The workflow passes whatever the repo secret holds, often plain postgresql://.

    Regression guard: this script first built its engine by hand with
    ``connect_args={"statement_cache_size": 0}``, which psycopg rejects with
    `invalid connection option`, so the report died before printing a number.
    """
    captured = {}

    class _Stop(Exception):
        pass

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        raise _Stop

    monkeypatch.setattr(check_module, "create_async_engine", fake_create_async_engine)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example:5432/postgres")

    with pytest.raises(_Stop):
        asyncio.run(check())

    assert captured["url"].drivername == "postgresql+asyncpg"
    assert captured["kwargs"]["connect_args"]["statement_cache_size"] == 0


# --- end to end over a real database --------------------------------------


async def _prepare_sqlite(monkeypatch, tmp_path, with_log_table=True):
    url = f"sqlite+aiosqlite:///{tmp_path / 'logcheck.db'}"
    monkeypatch.setenv("DATABASE_URL", url)

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if with_log_table:
            await conn.run_sync(TransparencyBase.metadata.create_all)
    return engine


async def test_check_reports_live_state(monkeypatch, tmp_path, capsys):
    engine = await _prepare_sqlite(monkeypatch, tmp_path)
    newer = NOW + timedelta(hours=5)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                TransparencyBase.metadata.tables["merkle_log_entries"].insert(),
                [_entry_row(0, NOW), _entry_row(1, newer)],
            )
            await conn.execute(
                Base.metadata.tables["raw_articles"].insert(),
                [
                    _article_row("https://example.test/a", log_index=0),
                    _article_row("https://example.test/b"),
                ],
            )
    finally:
        await engine.dispose()

    assert await check() == 0
    out = capsys.readouterr().out
    assert "Merkle log entries:      2" in out
    assert "Stamped articles:        1" in out
    assert "Total archived articles: 2" in out
    assert "Stamped share of archive: 50.00%" in out
    # max(timestamp), not whichever row came back first.
    assert newer.isoformat() in out
    assert NOW.isoformat() not in out
    assert out.strip().endswith("OK")


async def test_check_fails_when_the_log_cannot_be_read(monkeypatch, tmp_path, capsys):
    """An unreadable log is a defect; reporting it as fine is the bug this guards."""
    engine = await _prepare_sqlite(monkeypatch, tmp_path, with_log_table=False)
    await engine.dispose()

    assert await check() == 1
    out = capsys.readouterr().out
    assert "FAIL: could not read the transparency log state" in out
    assert "OK" not in out.split("FAIL")[-1]


async def test_check_reports_an_empty_log_without_failing(monkeypatch, tmp_path, capsys):
    """A day where nothing new was publishable must not turn a build red."""
    engine = await _prepare_sqlite(monkeypatch, tmp_path)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                Base.metadata.tables["raw_articles"].insert(),
                [_article_row("https://example.test/a")],
            )
    finally:
        await engine.dispose()

    assert await check() == 0
    out = capsys.readouterr().out
    assert "Merkle log entries:      0" in out
    assert "WARNING: the log is empty" in out
    assert out.strip().endswith("OK")
