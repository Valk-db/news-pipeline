#!/usr/bin/env python
"""Standalone cleanup script for stale stories.

Run via: uv run scripts/cleanup_stale.py

Can be scheduled as a separate cron job (daily/weekly) or triggered manually.
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.verification.cleanup import run_cleanup
from src.shared.database import init_db, get_session
from src.shared.config import get_settings


async def main():
    """Run cleanup job."""
    # Parse args
    pending_hours = 72
    blocked_hours = 168
    queued_hours = 0  # Default disabled (opt-in)

    for arg in sys.argv[1:]:
        if arg.startswith("--pending-hours="):
            pending_hours = int(arg.split("=")[1])
        elif arg.startswith("--blocked-hours="):
            blocked_hours = int(arg.split("=")[1])
        elif arg.startswith("--queued-hours="):
            queued_hours = int(arg.split("=")[1])
        elif arg == "--help":
            print("Usage: uv run scripts/cleanup_stale.py [--pending-hours=72] [--blocked-hours=168] [--queued-hours=0]")
            print("  --pending-hours: Hours after which PENDING stories expire (default: 72)")
            print("  --blocked-hours: Hours after which BLOCKED stories expire (default: 168)")
            print("  --queued-hours: Hours after which QUEUED stories expire (default: 0 = disabled, opt-in only)")
            return

    # Check database
    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL not configured")
        sys.exit(1)

    # Initialize DB
    await init_db()

    # Run cleanup
    async with get_session() as session:
        result = await run_cleanup(session, pending_hours, blocked_hours, queued_hours)

    # Print results
    print(f"Cleanup completed:")
    print(f"  Stories expired: {result.stories_expired}")
    print(f"  Orphaned units removed: {result.orphaned_units_removed}")
    print(f"  Stale links removed: {result.stale_links_removed}")

    for detail in result.details:
        print(f"  - {detail}")

    # Exit with error if anything failed (shouldn't happen with proper error handling)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())