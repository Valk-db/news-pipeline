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
from sqlalchemy.exc import ProgrammingError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.schema.models import RawArticle  # noqa: E402
from src.transparency.log import MerkleLogEntry  # noqa: E402


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


async def check() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not found in environment")
        return 1

    print("Reading transparency log state...")

    # Supabase pooler uses pgbouncer which doesn't support prepared statements
    engine = create_async_engine(database_url, echo=False, connect_args={"statement_cache_size": 0})
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as session:
            log_entries = (
                await session.execute(select(func.count()).select_from(MerkleLogEntry))
            ).scalar_one()
            stamped = (
                await session.execute(
                    select(func.count())
                    .select_from(RawArticle)
                    .where(RawArticle.log_index.is_not(None))
                )
            ).scalar_one()
            total = (
                await session.execute(select(func.count()).select_from(RawArticle))
            ).scalar_one()
            last_entry_at = (
                await session.execute(select(func.max(MerkleLogEntry.timestamp)))
            ).scalar_one()
    except ProgrammingError as exc:
        print(f"FAIL: transparency tables are not queryable: {exc}")
        print("The merkle log is the backbone of /proof; this is a schema defect.")
        await engine.dispose()
        return 1

    print(
        render_report(
            log_entries=log_entries,
            stamped_articles=stamped,
            total_articles=total,
            last_entry_at=last_entry_at,
        )
    )

    if stamped and last_entry_at is None:
        print("WARNING: articles are stamped but the log has no entries; inconsistent.")

    age_note_cutoff = datetime.now(timezone.utc).timestamp() - 48 * 3600
    if last_entry_at is not None:
        stamped_at = last_entry_at.timestamp()
        if stamped_at < age_note_cutoff:
            hours = (datetime.now(timezone.utc) - last_entry_at).total_seconds() / 3600
            print(
                f"WARNING: newest log entry is {hours:.1f}h old. The stamping workflow "
                f"is scheduled daily at 04:47 UTC; check whether it is failing."
            )

    await engine.dispose()
    print("OK")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Report the transparency log state")
    parser.parse_args()
    sys.exit(asyncio.run(check()))


if __name__ == "__main__":
    main()
