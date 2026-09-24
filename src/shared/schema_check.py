"""Read-only schema drift detection. No I/O at module level."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import Enum
from sqlalchemy.engine import Engine
from sqlalchemy.sql import text


def find_drift(
    metadata: Any,
    actual_columns: dict[str, set[str]],
    actual_enums: dict[str, set[str]],
) -> dict[str, list[Any]]:
    """
    Compare SQLAlchemy metadata against actual database schema.

    Args:
        metadata: SQLAlchemy MetaData object (typically Base.metadata)
        actual_columns: dict of table_name -> set of column names from information_schema
        actual_enums: dict of enum_type_name -> set of enum labels from pg_enum

    Returns:
        dict with keys: missing_tables, missing_columns, missing_enum_labels
        Each value is a sorted list.
    """
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    missing_enum_labels: list[tuple[str, str]] = []

    # First pass: collect all expected enum labels per type name (union across columns)
    expected_enum_labels: dict[str, set[str]] = defaultdict(set)

    for table in metadata.tables.values():
        for column in table.columns:
            col_type = column.type
            enum_type = None
            if isinstance(col_type, Enum):
                enum_type = col_type
            elif hasattr(col_type, "impl") and isinstance(col_type.impl, Enum):
                enum_type = col_type.impl

            if enum_type is not None and enum_type.name is not None:
                expected_enum_labels[enum_type.name].update(enum_type.enums)

    # Get expected tables from metadata
    expected_tables = set(metadata.tables.keys())
    actual_tables = set(actual_columns.keys())

    # Missing tables: in metadata but not in database
    for table_name in sorted(expected_tables - actual_tables):
        missing_tables.append(table_name)

    # For tables that exist in both, check columns
    for table_name in sorted(expected_tables & actual_tables):
        table = metadata.tables[table_name]
        expected_cols = {col.name for col in table.columns}
        actual_cols = actual_columns[table_name]
        for col_name in sorted(expected_cols - actual_cols):
            missing_columns.append((table_name, col_name))

    # Check enum columns for missing labels (using union of expected labels per type name)
    for enum_name, expected_labels in expected_enum_labels.items():
        actual_labels = actual_enums.get(enum_name, set())
        if actual_labels:
            for label in sorted(expected_labels - actual_labels):
                missing_enum_labels.append((enum_name, label))
        # If enum type not in actual_enums, we report nothing (per spec)

    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_enum_labels": missing_enum_labels,
    }


def has_drift(report: dict[str, list[Any]]) -> bool:
    """
    True iff missing_columns or missing_enum_labels is non-empty.
    Missing tables alone are NOT drift, because init_db() creates them.
    """
    return bool(report["missing_columns"]) or bool(report["missing_enum_labels"])


async def fetch_actual(engine: Engine) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """
    Fetch actual schema from the database. Read-only, no writes, no DDL.

    Returns:
        (actual_columns, actual_enums)
        actual_columns: table_name -> set of column names
        actual_enums: enum_type_name -> set of enum labels
    """
    actual_columns: dict[str, set[str]] = defaultdict(set)
    actual_enums: dict[str, set[str]] = defaultdict(set)

    async with engine.connect() as conn:
        # Get columns from information_schema
        result = await conn.execute(
            text(
                "SELECT table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
        )
        for row in result:
            actual_columns[row.table_name].add(row.column_name)

        # Get enum labels from pg_enum
        result = await conn.execute(
            text(
                "SELECT t.typname, e.enumlabel "
                "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE t.typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public')"
            )
        )
        for row in result:
            actual_enums[row.typname].add(row.enumlabel)

    return dict(actual_columns), dict(actual_enums)