"""Pure tests for schema_check.find_drift and has_drift. No DB, no fixtures, no settings, no app import."""

from sqlalchemy import Column, Enum, Integer, MetaData, Table

from src.schema.models import Base, RawArticle
from src.shared.schema_check import find_drift, has_drift


def test_no_drift_when_metadata_matches():
    """Test a: actual_columns from metadata, actual_enums from Enum.enums -> no drift."""
    # Build actual_columns from Base.metadata
    actual_columns = {
        table_name: {col.name for col in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }

    # Build actual_enums from each Enum column's enums
    actual_enums = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            col_type = column.type
            enum_type = None
            if hasattr(col_type, "enums"):  # Direct Enum
                enum_type = col_type
            elif hasattr(col_type, "impl") and hasattr(col_type.impl, "enums"):
                enum_type = col_type.impl

            if enum_type is not None and enum_type.name is not None:
                if enum_type.name not in actual_enums:
                    actual_enums[enum_type.name] = set()
                actual_enums[enum_type.name].update(enum_type.enums)

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    assert report["missing_columns"] == []
    assert report["missing_enum_labels"] == []
    assert has_drift(report) is False


def test_missing_column_detected():
    """Test b: Remove tier3_unit_count from stories -> missing_columns has entry, has_drift True."""
    actual_columns = {
        table_name: {col.name for col in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }

    # Remove tier3_unit_count from stories
    actual_columns["stories"].discard("tier3_unit_count")

    actual_enums = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            col_type = column.type
            enum_type = None
            if hasattr(col_type, "enums"):
                enum_type = col_type
            elif hasattr(col_type, "impl") and hasattr(col_type.impl, "enums"):
                enum_type = col_type.impl

            if enum_type is not None and enum_type.name is not None:
                if enum_type.name not in actual_enums:
                    actual_enums[enum_type.name] = set()
                actual_enums[enum_type.name].update(enum_type.enums)

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    assert ("stories", "tier3_unit_count") in report["missing_columns"]
    assert has_drift(report) is True


def test_missing_enum_label_detected():
    """Test c: actual_enums missing TIER4 -> missing_enum_labels has entry, has_drift True."""
    # Verify the enum name first
    source_tier_column = RawArticle.__table__.c.source_tier
    assert source_tier_column.type.name == "sourcetier"

    actual_columns = {
        table_name: {col.name for col in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }

    # Actual enums missing TIER4
    actual_enums = {"sourcetier": {"TIER1", "TIER2", "TIER3"}}

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    assert ("sourcetier", "TIER4") in report["missing_enum_labels"]
    assert has_drift(report) is True


def test_missing_table_is_drift():
    """Test d: Delete stories from actual_columns -> missing_tables has stories, has_drift True (missing tables are now drift)."""
    actual_columns = {
        table_name: {col.name for col in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }

    # Remove stories table entirely
    del actual_columns["stories"]

    actual_enums = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            col_type = column.type
            enum_type = None
            if hasattr(col_type, "enums"):
                enum_type = col_type
            elif hasattr(col_type, "impl") and hasattr(col_type.impl, "enums"):
                enum_type = col_type.impl

            if enum_type is not None and enum_type.name is not None:
                if enum_type.name not in actual_enums:
                    actual_enums[enum_type.name] = set()
                actual_enums[enum_type.name].update(enum_type.enums)

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    assert "stories" in report["missing_tables"]
    assert has_drift(report) is True


def test_extra_columns_ignored():
    """Test e: Extra columns in actual_columns not in metadata -> ignored, has_drift False."""
    actual_columns = {
        table_name: {col.name for col in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }

    # Add extra column that doesn't exist in metadata
    actual_columns["stories"].add("extra_column_that_does_not_exist")

    actual_enums = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            col_type = column.type
            enum_type = None
            if hasattr(col_type, "enums"):
                enum_type = col_type
            elif hasattr(col_type, "impl") and hasattr(col_type.impl, "enums"):
                enum_type = col_type.impl

            if enum_type is not None and enum_type.name is not None:
                if enum_type.name not in actual_enums:
                    actual_enums[enum_type.name] = set()
                actual_enums[enum_type.name].update(enum_type.enums)

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    assert has_drift(report) is False


def test_enum_absent_from_actual_enums_ignored():
    """Test f: Enum type absent from actual_enums -> no missing_enum_labels entry."""
    actual_columns = {
        table_name: {col.name for col in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }

    # Empty actual_enums (enum type not present)
    actual_enums = {}

    report = find_drift(Base.metadata, actual_columns, actual_enums)

    # Should not report missing enum labels for sourcetier since it's not in actual_enums
    assert ("sourcetier", "TIER4") not in report["missing_enum_labels"]
    assert has_drift(report) is False


def test_shared_enum_type_name_union_of_labels():
    """Test g: Two tables share enum name="status" but have different labels.
    With actual_enums = {"status": {"A", "B"}}, ("status", "C") is in missing_enum_labels
    (expected labels for a type name are the union across columns)."""
    metadata = MetaData()

    # Table 1: status enum with A, B
    Table(
        "table_a",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("status", Enum("A", "B", name="status")),
    )

    # Table 2: status enum with C (same type name, different labels)
    Table(
        "table_b",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("status", Enum("C", name="status")),
    )

    actual_columns = {
        "table_a": {"id", "status"},
        "table_b": {"id", "status"},
    }

    # Actual DB only has A, B for the shared type "status"
    actual_enums = {"status": {"A", "B"}}

    report = find_drift(metadata, actual_columns, actual_enums)

    # Expected labels for "status" are the union: A, B, C
    # Missing: C
    assert ("status", "C") in report["missing_enum_labels"]
    assert has_drift(report) is True


def test_real_models_no_enum_name_collisions():
    """Test h: Guard on real models - group every Enum column by type.name;
    assert no name is shared by columns whose type.enums differ.
    Failure message lists colliding type name and table.column entries.
    This passes on main today."""
    from collections import defaultdict

    enum_by_name: dict[str, list[tuple[str, str, tuple]]] = defaultdict(list)

    for table_name, table in Base.metadata.tables.items():
        for column in table.columns:
            col_type = column.type
            enum_type = None
            if hasattr(col_type, "enums"):
                enum_type = col_type
            elif hasattr(col_type, "impl") and hasattr(col_type.impl, "enums"):
                enum_type = col_type.impl

            if enum_type is not None and enum_type.name is not None:
                enum_by_name[enum_type.name].append(
                    (table_name, column.name, tuple(enum_type.enums))
                )

    collisions = []
    for type_name, entries in enum_by_name.items():
        if len(entries) > 1:
            # Check if all entries have the same enum labels
            first_labels = entries[0][2]
            for table_name, col_name, labels in entries[1:]:
                if labels != first_labels:
                    collisions.append((type_name, entries))
                    break

    if collisions:
        msg_lines = ["Enum name collisions detected (same type.name, different labels):"]
        for type_name, entries in collisions:
            msg_lines.append(f"  Type '{type_name}':")
            for table_name, col_name, labels in entries:
                msg_lines.append(f"    {table_name}.{col_name}: {labels}")
        raise AssertionError("\n".join(msg_lines))