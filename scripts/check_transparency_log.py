#!/usr/bin/env python3
"""Report the state of the transparency log. Read-only.

Runs after the stamping workflow (and is useful by hand) so the effect of a
stamping run is visible in the Actions log rather than inferred. The bug this
was written for was silent: the scheduled pipeline never called the stamping
path, the log simply stopped growing, and every /proof page said "pending" with
no indication that anything was wrong.

This reports and never gates. A day with no new articles is a legitimate
outcome (the four evidence feeds can yield nothing new within the 10-fetch
budget) and must not fail a build. A missing merkle_log_entries table IS a real
defect -- the whole transparency feature is dead without it -- so that exits
non-zero.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.schema.models import RawArticle  # noqa: E402
from src.shared.database import prepare_database_url  # noqa: E402
from src.transparency.log import MerkleLogEntry  # noqa: E402

# The daily cadence of .github/workflows/transparency-stamp.yml. Named here so
# the report can point at the schedule it is checking, and so a test can assert
# the two agree.
SCHEDULED_STAMP_CRON = "04:47 UTC"
STALE_HOURS = 48


def _as_utc(value: datetime | None) -> datetime | None:
    """Postgres hands back aware datetimes; SQLite hands back naive ones.

    Comparing a naive value against ``datetime.now(timezone.utc)`` raises, which
    would turn a working report into a traceback on any non-Postgres database.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def render_report(
    *,
    log_entries: int,
    stamped_articles: int,
    total_articles: int,
    last_entry_at: datetime | None,
) -> str:
    """Format the numbers. Pure, so the thresholds are unit-testable.

    Deliberately does not decide pass/fail on emptiness: an empty log on a fresh
    database is correct, and the signer already refuses to sign one.
    """
    lines = [
        f"Merkle log entries:      {log_entries}",
        f"Stamped articles:        {stamped_articles}",
        f"Total archived articles: {total_articles}",
        f"Newest log entry:        {last_entry_at.isoformat() if last_entry_at else 'none'}",
    ]
    if total_articles:
        pct = 100.0 * stamped_articles / total_articles
        lines.append(f"Stamped share of archive: {pct:.2f}%")
    if log_entries == 0:
        lines.append(
            "WARNING: the log is empty. The checkpoint signer fails closed on an "
            "empty log, so no checkpoint will be produced."
        )
    return "\n".join(lines)


async def read_state(session) -> dict:
    """Read the counters this report is about. Index-only counts, no row bodies."""
    return {
        "log_entries": (
            await session.execute(select(func.count()).select_from(MerkleLogEntry))
        ).scalar_one(),
        "stamped_articles": (
            await session.execute(
                select(func.count())
                .select_from(RawArticle)
                .where(RawArticle.log_index.is_not(None))
            )
        ).scalar_one(),
        "total_articles": (
            await session.execute(select(func.count()).select_from(RawArticle))
        ).scalar_one(),
        "last_entry_at": _as_utc(
            (await session.execute(select(func.max(MerkleLogEntry.timestamp)))).scalar_one()
        ),
    }


def staleness_warning(last_entry_at, now=None, stale_hours=STALE_HOURS) -> str | None:
    """Warn when the newest log entry is older than the daily cadence allows.

    This is the symptom of the bug this script was written for: a scheduled
    stamping run that has quietly stopped. Pure, so the threshold is testable.
    """
    if last_entry_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    age_hours = (now - last_entry_at).total_seconds() / 3600
    if age_hours < stale_hours:
        return None
    return (
        f"WARNING: newest log entry is {age_hours:.1f}h old. The stamping workflow "
        f"is scheduled daily at {SCHEDULED_STAMP_CRON}; check whether it is failing."
    )


async def check() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not found in environment")
        return 1

    print("Reading transparency log state...")

    # Reuse the app's own URL translation: a plain ``postgresql://`` from a
    # dashboard has to become asyncpg, and the Supabase pooler cannot keep
    # prepared statements. Building the engine by hand here is how this script
    # first shipped broken (psycopg rejects ``statement_cache_size``).
    url, connect_args = prepare_database_url(database_url)
    engine = create_async_engine(url, echo=False, connect_args=connect_args)
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as session:
            state = await read_state(session)
    except SQLAlchemyError as exc:
        # Any failure to read the counters means the state of the log is
        # unknown, and "unknown" must not be reported as "fine".
        print(f"FAIL: could not read the transparency log state: {exc}")
        print("The merkle log is the backbone of /proof; this is a schema or "
              "connectivity defect.")
        await engine.dispose()
        return 1

    print(render_report(**state))

    if state["stamped_articles"] and state["last_entry_at"] is None:
        print("WARNING: articles are stamped but the log has no entries; inconsistent.")

    warning = staleness_warning(state["last_entry_at"])
    if warning:
        print(warning)

    await engine.dispose()
    print("OK")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Report the transparency log state")
    parser.parse_args()
    sys.exit(asyncio.run(check()))


if __name__ == "__main__":
    main()
