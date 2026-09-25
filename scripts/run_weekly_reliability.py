#!/usr/bin/env python
"""Weekly reliability snapshot run: computes per-source reliability
snapshots for the previous UTC day.

Used by .github/workflows/weekly-enrichment.yml.

Usage:
    uv run python scripts/run_weekly_reliability.py
"""

import asyncio
from datetime import datetime, timezone

from src.shared.database import init_db, get_session
from src.reliability.consensus_analyzer import compute_daily_reliability_snapshots


async def main() -> None:
    await init_db()
    async with get_session() as session:
        yesterday = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        count = await compute_daily_reliability_snapshots(session, yesterday)
        print(f"Created {count} reliability snapshots")


if __name__ == "__main__":
    asyncio.run(main())