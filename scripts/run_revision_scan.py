#!/usr/bin/env python
"""Revision scan runner: catch stealth edits and record corrections.

Re-fetches recently ingested articles, diffs what came back against the last state we
saw, and writes one article_revisions row per observed change -- classified as
acknowledged (a correction/update notice, or a newer displayed timestamp) or stealth.
Nothing is updated or deleted; articles that did not change produce no rows.

Usage:
    uv run python scripts/run_revision_scan.py --dry-run           # Preview without writing
    uv run python scripts/run_revision_scan.py --limit=25          # Smaller batch
    uv run python scripts/run_revision_scan.py --window-hours=24   # Only today's articles
"""

import argparse
import asyncio
import logging

from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker
from src.verification.revisions import (
    DEFAULT_RESCAN_AFTER_HOURS,
    DEFAULT_SCAN_LIMIT,
    DEFAULT_SCAN_WINDOW_HOURS,
    RevisionScanResult,
    scan_revisions,
)


async def run(
    dry_run: bool = False,
    limit: int = DEFAULT_SCAN_LIMIT,
    window_hours: int = DEFAULT_SCAN_WINDOW_HOURS,
    rescan_after_hours: int = DEFAULT_RESCAN_AFTER_HOURS,
) -> int:
    """Open a session, run the revision scan, print the summary. Returns exit code."""
    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL not configured")
        return 1

    engine = _get_engine()
    if engine is None:
        print("ERROR: Could not create engine")
        return 1

    session_maker = _get_session_maker()
    if session_maker is None:
        print("ERROR: Could not create session maker")
        return 1

    async with session_maker() as session:
        result: RevisionScanResult = await scan_revisions(
            session,
            window_hours=window_hours,
            rescan_after_hours=rescan_after_hours,
            limit=limit,
            dry_run=dry_run,
        )

    print(f"{'Would record' if dry_run else 'Recorded'}:")
    print(f"  Articles scanned: {result.articles_scanned}")
    print(f"  Revisions: {result.revisions_created}")
    print(f"    Acknowledged (notice or newer timestamp): {result.revisions_acknowledged}")
    print(f"    Stealth (changed, nothing admitted it): {result.revisions_stealth}")
    print(f"  Stealth rate: {result.stealth_rate:.0%}")
    print(f"  Corrections recorded: {result.corrections_recorded}")
    print(f"  Articles unchanged: {result.articles_unchanged}")
    print(f"  Revisions without a diff (no previous text): {result.revisions_without_diff}")
    print(f"  Fetch failures: {result.fetch_failures}")
    print(f"  Skipped (checked recently): {result.skipped_recent}")

    for detail in result.details:
        print(f"  - {detail}")

    if dry_run:
        print("Dry run: nothing was written.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Re-fetch recent articles and record every revision found"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SCAN_LIMIT,
        help=f"Maximum articles to re-fetch per run (default: {DEFAULT_SCAN_LIMIT})",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_SCAN_WINDOW_HOURS,
        help=(
            "Only re-check articles ingested within this window "
            f"(default: {DEFAULT_SCAN_WINDOW_HOURS})"
        ),
    )
    parser.add_argument(
        "--rescan-after-hours",
        type=int,
        default=DEFAULT_RESCAN_AFTER_HOURS,
        help=(
            "Skip an article checked within this many hours, 0 disables "
            f"(default: {DEFAULT_RESCAN_AFTER_HOURS})"
        ),
    )
    args = parser.parse_args()

    raise SystemExit(
        asyncio.run(
            run(
                dry_run=args.dry_run,
                limit=args.limit,
                window_hours=args.window_hours,
                rescan_after_hours=args.rescan_after_hours,
            )
        )
    )
