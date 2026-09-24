"""Schema drift check CLI."""

import asyncio
import os
import sys

# Add project root to path so `src` imports work both as `uv run python scripts/check_schema.py`
# and `uv run python -m scripts.check_schema` without PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.shared.config import get_settings
from src.shared.database import _get_engine
from src.schema.models import Base
from src.shared.schema_check import fetch_actual, find_drift, has_drift


async def main() -> int:
    settings = get_settings()
    if not settings.has_database:
        print("No database configured, skipping schema check")
        return 0

    engine = _get_engine()
    if engine is None:
        print("No database engine available, skipping schema check")
        return 0

    # Verify it's PostgreSQL
    if engine.dialect.name != "postgresql":
        print(f"Non-PostgreSQL dialect ({engine.dialect.name}), skipping schema check")
        return 0

    try:
        actual_columns, actual_enums = await fetch_actual(engine)
    except Exception as exc:
        # Never print DATABASE_URL or connection details
        print(f"{type(exc).__name__}: connection error")
        return 2

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    # Print missing columns
    for table, column in report["missing_columns"]:
        print(f"MISSING COLUMN {table}.{column}")

    # Print missing enum labels
    for enum_type, label in report["missing_enum_labels"]:
        print(f"MISSING ENUM LABEL {enum_type}.{label}")

    # Print missing tables
    for table in report["missing_tables"]:
        print(f"INFO table will be created by init_db: {table}")

    # Print existing sourcetier labels for visibility
    sourcetier_labels = actual_enums.get("sourcetier", set())
    if sourcetier_labels:
        print(f"Existing sourcetier labels: {', '.join(sorted(sourcetier_labels))}")

    if has_drift(report):
        print("Schema drift detected. Apply the SQL files in supabase/migrations/ and re-run.")
        return 1

    print("No schema drift.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))