"""Enable Row Level Security (RLS) on all tables with no policies.

This closes the anonymous REST API path while the backend (connecting as
service role) bypasses RLS entirely.
"""

import asyncio
from sqlalchemy import text
from src.shared.database import _get_engine
from src.shared.config import get_settings


async def main():
    settings = get_settings()
    if not settings.has_database:
        print("No database configured, skipping RLS setup")
        return

    engine = _get_engine()
    if engine is None:
        print("Could not create engine")
        return

    tables = [
        "raw_articles",
        "reporting_units",
        "stories",
        "story_unit_links",
        "curated_posts",
        "status_log",
    ]

    async with engine.begin() as conn:
        for table in tables:
            # Enable RLS
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            print(f"Enabled RLS on {table}")

            # Verify no policies exist (should be 0 rows)
            result = await conn.execute(text(f"""
                SELECT policyname FROM pg_policies WHERE tablename = '{table}'
            """))
            policies = result.scalars().all()
            if policies:
                print(f"  WARNING: {table} has {len(policies)} policies: {policies}")
            else:
                print(f"  OK: {table} has no policies (anon path closed)")

    print("RLS setup complete!")


if __name__ == "__main__":
    asyncio.run(main())