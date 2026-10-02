"""The five dev tables whose SERIAL default CREATE TABLE IF NOT EXISTS skipped.

news-pipeline-dev holds status_log, claim_evidence, entity_edges,
source_reliability_snapshots and story_topic_groups as `id integer NOT NULL`
with column_default IS NULL, so the first insert into each one dies on a null
id. This exercises supabase/migrations/20261002030000_missing_id_defaults.sql
against a scratch schema: it attaches a nextval default, applies again without
drifting, leaves a table that already has a default alone, and skips tables
that are not there at all.

Needs a real Postgres (CI sets DATABASE_URL) and skips without one, since the
migration is Postgres DDL and SQLite cannot run it.
"""

import os
from pathlib import Path

import asyncpg
import pytest

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "migrations"
    / "20261002030000_missing_id_defaults.sql"
)

TABLES = (
    "status_log",
    "claim_evidence",
    "entity_edges",
    "source_reliability_snapshots",
    "story_topic_groups",
)

SCHEMA = "w2ids_migration_scratch"


def migration_sql(schema: str) -> str:
    """The migration with its `public.` qualifier aimed at a scratch schema.

    The tables live in public on dev, so the migration names that schema
    literally. Swapping the prefix is what lets the test run against CI's
    database without touching a table the app uses.
    """
    body = MIGRATION.read_text().split("DO $$", 1)[1]
    return "DO $$" + body.replace("public.", f"{schema}.")


async def create_broken(conn: asyncpg.Connection, table: str) -> None:
    """A table shaped like the ones dev actually has: integer id, no default."""
    await conn.execute(f"CREATE TABLE {SCHEMA}.{table} (id integer NOT NULL PRIMARY KEY, payload text)")


async def id_default(conn: asyncpg.Connection, table: str):
    return await conn.fetchval(
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'id'"
        " JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
        " WHERE n.nspname = $1 AND c.relname = $2",
        SCHEMA,
        table,
    )


@pytest.fixture
async def scratch():
    dsn = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        pytest.skip("no DATABASE_URL: the migration is Postgres DDL")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # cause varies: no server, refused, bad password
        pytest.skip(f"cannot reach Postgres: {exc}")
    await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {SCHEMA}")
    try:
        yield conn
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()


async def test_every_table_gets_a_usable_default(scratch):
    for table in TABLES:
        await create_broken(scratch, table)
    await scratch.execute(migration_sql(SCHEMA))

    for table in TABLES:
        assert await id_default(scratch, table) == f"nextval('{SCHEMA}.{table}_id_seq'::regclass)"
        first = await scratch.fetchval(f"INSERT INTO {SCHEMA}.{table} (payload) VALUES ('a') RETURNING id")
        second = await scratch.fetchval(f"INSERT INTO {SCHEMA}.{table} (payload) VALUES ('b') RETURNING id")
        assert second > first, f"{table} hands out the same id twice"


async def test_applying_it_twice_changes_nothing(scratch):
    for table in TABLES:
        await create_broken(scratch, table)
    await scratch.execute(migration_sql(SCHEMA))
    await scratch.execute(f"INSERT INTO {SCHEMA}.status_log (payload) VALUES ('between runs')")
    defaults = [await id_default(scratch, table) for table in TABLES]

    await scratch.execute(migration_sql(SCHEMA))

    assert [await id_default(scratch, table) for table in TABLES] == defaults
    # The row written between the two applies is still there, and the next id
    # continues past it rather than colliding with it.
    rows = await scratch.fetch(f"SELECT id, payload FROM {SCHEMA}.status_log ORDER BY id")
    assert [r["payload"] for r in rows] == ["between runs"]
    assert await scratch.fetchval(f"INSERT INTO {SCHEMA}.status_log (payload) VALUES ('after') RETURNING id") > max(
        r["id"] for r in rows
    )


async def test_a_table_that_already_has_a_default_is_left_alone(scratch):
    await scratch.execute(f"CREATE TABLE {SCHEMA}.entity_edges (id SERIAL PRIMARY KEY, payload text)")
    existing = await scratch.fetchval(
        f"INSERT INTO {SCHEMA}.entity_edges (payload) VALUES ('first') RETURNING id"
    )

    await scratch.execute(migration_sql(SCHEMA))

    assert await id_default(scratch, "entity_edges") == f"nextval('{SCHEMA}.entity_edges_id_seq'::regclass)"
    assert (
        await scratch.fetchval(f"INSERT INTO {SCHEMA}.entity_edges (payload) VALUES ('second') RETURNING id")
        == existing + 1
    )


async def test_the_sequence_follows_the_column(scratch):
    await create_broken(scratch, "story_topic_groups")
    await scratch.execute(migration_sql(SCHEMA))
    await scratch.execute(f"DROP TABLE {SCHEMA}.story_topic_groups")

    assert not await scratch.fetchval(
        "SELECT to_regclass($1)", f"{SCHEMA}.story_topic_groups_id_seq"
    ), "OWNED BY should have dropped the sequence with its column"


async def test_tables_that_do_not_exist_are_skipped(scratch):
    await create_broken(scratch, "entity_edges")

    await scratch.execute(migration_sql(SCHEMA))

    assert await id_default(scratch, "entity_edges") is not None
    assert not await scratch.fetchval(
        "SELECT to_regclass($1)", f"{SCHEMA}.claim_evidence_id_seq"
    )