#!/usr/bin/env python3
"""Check that tier-1 articles have been ingested in the last 30 hours.

This is a read-only check that runs as a final workflow step (if: always()).
Exits non-zero if zero tier-1 rows in RawArticle.fetched_at within 30h.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from src.schema.models import RawArticle, SourceTier


async def check_freshness(hours: int = 30) -> int:
    """Check freshness of tier-1 articles.

    Returns:
        0 if OK, 1 if no tier-1 articles in window
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        # Try to load from .env
        from dotenv import load_dotenv
        load_dotenv()
        database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("ERROR: DATABASE_URL not found in environment or .env")
        return 1

    print(f"Checking tier-1 article freshness (last {hours}h)...")

    # Supabase pooler uses pgbouncer which doesn't support prepared statements
    engine = create_async_engine(database_url, echo=False, connect_args={"statement_cache_size": 0})
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with async_session() as session:
        # Count tier-1 articles in window
        stmt = select(func.count(RawArticle.id)).where(
            RawArticle.fetched_at >= cutoff,
            RawArticle.source_tier == SourceTier.TIER1,
        )
        result = await session.execute(stmt)
        tier1_count = result.scalar() or 0

        print(f"Tier-1 articles in last {hours}h: {tier1_count}")

        if tier1_count == 0:
            print(f"FAIL: Zero tier-1 articles in the last {hours}h")
            print("This indicates the ingestion pipeline is not producing tier-1 content.")
            await engine.dispose()
            return 1

        # Also show per-domain breakdown for debugging
        stmt = (
            select(RawArticle.source_domain, func.count(RawArticle.id))
            .where(
                RawArticle.fetched_at >= cutoff,
                RawArticle.source_tier == SourceTier.TIER1,
            )
            .group_by(RawArticle.source_domain)
            .order_by(func.count(RawArticle.id).desc())
        )
        result = await session.execute(stmt)
        print("\nPer-domain breakdown:")
        for domain, count in result.all():
            print(f"  {domain}: {count}")

    await engine.dispose()
    print("OK: Tier-1 articles are fresh")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Check tier-1 article freshness")
    parser.add_argument("--hours", type=int, default=30, help="Hours to look back (default: 30)")
    args = parser.parse_args()

    exit_code = asyncio.run(check_freshness(args.hours))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()