#!/usr/bin/env python3
"""Apply every SQL file in supabase/migrations/, oldest first.

This is the schema authority. `Base.metadata.create_all` used to race these files
and always won, because every ingest and weekly job called it first; it is gone now,
so these files are the only thing that changes the schema. Every one of them is
idempotent, which is what makes re-running the whole set safe: the runner applies
all of them every time rather than tracking a ledger of what it already did.

Usage:
    python scripts/migrate.py

Uses DATABASE_URL, like everything else here. Exits 1 on the first file that fails,
naming the file, so a broken migration stops the run instead of leaving a half-applied
schema behind.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to path so `src` imports work both as `python scripts/migrate.py`
# and `python -m scripts.migrate` without PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.config import get_settings
from src.shared.database import _get_engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


async def main() -> int:
    if not get_settings().has_database:
        print("no DATABASE_URL configured, nothing to migrate")
        return 1

    engine = _get_engine()
    if engine is None or engine.dialect.name != "postgresql":
        print("no PostgreSQL engine available, nothing to migrate")
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"no migration files under {MIGRATIONS_DIR}")
        return 1

    async with engine.connect() as conn:
        # The raw asyncpg connection, not SQLAlchemy's execute: asyncpg refuses to put
        # several commands into a prepared statement, and a migration file is many.
        raw = (await conn.get_raw_connection()).driver_connection
        for path in files:
            try:
                await raw.execute(path.read_text())
            except Exception as exc:
                # Never print the connection string, so only the exception type is named.
                print(f"{path.name}: FAILED {type(exc).__name__}: {exc}")
                await conn.rollback()
                return 1
            print(f"{path.name}: ok")
        await conn.commit()

    print(f"{len(files)} migration(s) applied")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))