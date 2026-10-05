#!/usr/bin/env python3
"""Report the pipeline's loose ends: rows that stopped somewhere and were never resolved.

Five buckets, one printed line each, in this order:

    raw_articles pending older than 24h   written, then never processed
    reporting_units with no story         clustered, then never grouped
    stories with no event                 grouped, then never located
    events with no story                  located against a story that is gone
    dead_letters last 24h                 items the pipeline gave up on, by design

None of these are failures on their own. A fresh ingest legitimately has articles that have
not been clustered yet, so the numbers are a trend to watch, not a gate. A bucket whose table
is missing, or a database that cannot be reached, *is* a failure: the report is incomplete,
and an incomplete report in a scheduled job is indistinguishable from a healthy one. So the
script exits 0 when all five buckets printed real numbers and 1 otherwise, which makes it
usable as the final step of a workflow without inventing a threshold for the counts.

Usage:
    python scripts/check_orphans.py [--hours 24]
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# Add project root to path so `src` imports work both as `python scripts/check_orphans.py`
# and `python -m scripts.check_orphans` without PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import DeadLetter, Event, RawArticle, ReportingUnit, Story, StoryUnitLink
from src.shared.config import get_settings
from src.shared.database import get_session
from src.shared.ledger import PENDING_TERMINAL_STATE

# How far back the "should have moved by now" buckets look.
WINDOW_HOURS = 24


def _pending_articles(cutoff: datetime):
    """Articles written before the cutoff that no stage has moved off pending."""
    return select(func.count(RawArticle.id)).where(
        RawArticle.terminal_state == PENDING_TERMINAL_STATE,
        RawArticle.fetched_at < cutoff,
    )


def _units_without_story(cutoff: datetime):
    """Reporting units that never got a story_unit_links row."""
    return (
        select(func.count(ReportingUnit.id))
        .outerjoin(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.id.is_(None))
    )


def _stories_without_event(cutoff: datetime):
    """Stories that were never located, so they have no event."""
    return (
        select(func.count(Story.id))
        .outerjoin(Event, Event.story_id == Story.id)
        .where(Event.id.is_(None))
    )


def _events_without_story(cutoff: datetime):
    """Events whose story_id is NULL, or points at a story that no longer exists.

    The FK is ON DELETE CASCADE, so the dangling case should be impossible; it is counted
    anyway because an orphan event on the map is worth knowing about.
    """
    return (
        select(func.count(Event.id))
        .outerjoin(Story, Story.id == Event.story_id)
        .where(Story.id.is_(None))
    )


def _dead_letters(cutoff: datetime):
    """Dead letters written inside the window."""
    return select(func.count(DeadLetter.id)).where(DeadLetter.created_at >= cutoff)


# Report order. Each entry is a label for the line and a function returning the count
# statement. Every builder takes the window cutoff so the buckets stay uniform, but only the
# two that are about recency use it.
BUCKETS: list[tuple[Callable[[int], str], Callable[[datetime], Any]]] = [
    (lambda hours: f"raw_articles pending older than {hours}h", _pending_articles),
    (lambda hours: "reporting_units with no story", _units_without_story),
    (lambda hours: "stories with no event", _stories_without_event),
    (lambda hours: "events with no story", _events_without_story),
    (lambda hours: f"dead_letters last {hours}h", _dead_letters),
]


async def gather(session: AsyncSession, hours: int = WINDOW_HOURS) -> tuple[list[str], int]:
    """Return one report line per bucket, in report order, and how many could not be counted.

    A bucket that fails prints its own error line and does not stop the others, so one
    missing table cannot hide the rest of the report. The count is what the caller
    turns into an exit code.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    lines: list[str] = []
    failed = 0
    for label, build in BUCKETS:
        try:
            count = await session.execute(build(cutoff))
            lines.append(f"{label(hours)}: {int(count.scalar_one())}")
        except Exception as exc:
            failed += 1
            lines.append(f"{label(hours)}: error {type(exc).__name__}")
            print(f"  (query failed for {label(hours)}: {type(exc).__name__})", file=sys.stderr)
    return lines, failed


async def report(hours: int = WINDOW_HOURS) -> int:
    """Print the report. Returns 1 if any bucket could not be counted, else 0."""
    if not get_settings().has_database:
        print("no DATABASE_URL configured, nothing to report")
        return 1
    try:
        async with get_session() as session:
            lines, failed = await gather(session, hours)
            for line in lines:
                print(line)
    except Exception as exc:
        # Never print the connection string, so only the exception type is reported.
        print(f"could not read the database: {type(exc).__name__}")
        return 1
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Report pipeline orphans")
    parser.add_argument(
        "--hours",
        type=int,
        default=WINDOW_HOURS,
        help=f"Hours to look back for pending articles and dead letters (default: {WINDOW_HOURS})",
    )
    args = parser.parse_args()
    return asyncio.run(report(args.hours))


if __name__ == "__main__":
    sys.exit(main())
