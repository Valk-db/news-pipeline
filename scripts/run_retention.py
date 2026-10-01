#!/usr/bin/env python
"""Retention job runner: reclaim space on the Supabase free tier (500MB).

Deletes stale embeddings and NULLs old article body_text, keeping every row and
every hash that ingestion dedup depends on.

Usage:
    uv run python scripts/run_retention.py --dry-run          # Preview counts/estimate
    uv run python scripts/run_retention.py                     # Run with defaults (90 days)
    uv run python scripts/run_retention.py --retention-days=30 # Tighten the window
"""

import argparse
import asyncio
import logging

from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker
from src.verification.retention import DEFAULT_RETENTION_DAYS, RetentionResult, run_retention


async def run(dry_run: bool = False, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Open a session, run the retention job, print the summary. Returns exit code."""
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
        result: RetentionResult = await run_retention(
            session,
            retention_days=retention_days,
            dry_run=dry_run,
        )

    print(f"{'Would reclaim' if dry_run else 'Reclaimed'}:")
    print(f"  Article embeddings deleted: {result.article_embeddings_deleted}")
    print(f"  Story embeddings deleted: {result.story_embeddings_deleted}")
    print(f"  body_text cleared on articles: {result.raw_articles_body_text_cleared}")
    print(f"  Estimated space reclaimed: {result.estimated_bytes_reclaimed / (1024 * 1024):.2f} MB")

    for detail in result.details:
        print(f"  - {detail}")

    if dry_run:
        print("Dry run: nothing was written.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Reclaim space from embeddings and old article bodies")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts without writing")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Days before embeddings/body_text are reclaimed (default: {DEFAULT_RETENTION_DAYS})",
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(run(dry_run=args.dry_run, retention_days=args.retention_days)))