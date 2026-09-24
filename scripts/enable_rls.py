"""Enable Row Level Security (RLS) on all tables in the models with no policies.

This closes the anonymous REST API path while the backend (connecting as
service role) bypasses RLS entirely.
"""

import asyncio
from sqlalchemy import text
from src.shared.database import _get_engine
from src.shared.config import get_settings
from src.schema.models import Base


async def main():
    settings = get_settings()
    if not settings.has_database:
        print("No database configured, skipping RLS setup")
        return

    engine = _get_engine()
    if engine is None:
        print("Could not create engine")
        return

    tables = sorted(Base.metadata.tables.keys())

    async with engine.begin() as conn:
        for table in tables:
            # Enable RLS (quote the identifier)
            await conn.execute(text(f'ALTER TABLE IF EXISTS "{table}" ENABLE ROW LEVEL SECURITY'))
            print(f"Enabled RLS on {table}")

            # Verify no policies exist (should be 0 rows) - parametrize table name
            result = await conn.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = :t"),
                {"t": table}
            )
            policies = result.scalars().all()
            if policies:
                print(f"  WARNING: {table} has {len(policies)} policies: {policies}")
            else:
                print(f"  OK: {table} has no policies (anon path closed)")

    print("RLS setup complete!")


if __name__ == "__main__":
    asyncio.run(main())