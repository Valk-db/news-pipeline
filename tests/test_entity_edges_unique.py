"""entity_edges needs a business-key UNIQUE constraint, not just its primary key.

The business key of an edge is the 5-tuple (subject_type, subject_id,
predicate, object_type, object_id). Until 20261002040000_entity_edges_unique.sql
the table had only `id` as its key, so the same edge could be stored many times
over. Both writers are check-then-insert and so were idempotent against
themselves but not against a concurrent run: two runs can both SELECT, both see
nothing, and both INSERT.

Covers the four things that has to be true:

- the model declares uq_entity_edge over exactly those five columns, and the
  three pre-existing indexes survive;
- the migration applies twice without error and without drifting;
- the migration deletes duplicate 5-tuples keeping the earliest row *before*
  adding the constraint, so a re-apply against a table that has picked up a
  duplicate cannot fail (ADD CONSTRAINT would);
- a duplicate 5-tuple raises IntegrityError, while a row differing in any one of
  the five columns is still accepted -- the constraint must not be wider than
  the business key.

The migration tests need a real Postgres (CI sets DATABASE_URL) and skip without
one, since the migration is Postgres DDL and SQLite cannot run it. The
constraint behaviour and the narrative backfill tests run on in-memory SQLite,
which is enough because the constraint is part of the model metadata.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from src.schema.models import Base, EdgePredicate, EntityEdge, Story
from src.verification.narrative import link_narrative_arcs

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "migrations"
    / "20261002040000_entity_edges_unique.sql"
)

SCHEMA = "w3edges_uq_scratch"

# The 5-tuple, in the order the constraint declares it.
BUSINESS_KEY = ("subject_type", "subject_id", "predicate", "object_type", "object_id")

# Postgres stores the enum by name, not by EdgePredicate's .value.
PREDICATE_LABEL = "SAME_EVENT_AS"

INSERT = (
    "INSERT INTO {s}.entity_edges "
    "(subject_type, subject_id, predicate, object_type, object_id, confidence, created_at) "
    "VALUES ('story', $1, CAST($2 AS {s}.edgepredicate), 'story', $3, $4, now()) RETURNING id"
)


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


def test_model_declares_the_business_key_unique_constraint():
    """uq_entity_edge exists and covers exactly the 5-tuple, in order."""
    named = [
        c
        for c in EntityEdge.__table__.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_entity_edge"
    ]
    assert len(named) == 1, "expected exactly one constraint named uq_entity_edge"

    (constraint,) = named
    assert tuple(col.name for col in constraint.columns) == BUSINESS_KEY


def test_model_keeps_the_three_existing_indexes():
    """Adding the constraint must not disturb the lookup indexes."""
    index_names = {i.name for i in EntityEdge.__table__.indexes}
    assert {
        "ix_entity_edges_subject",
        "ix_entity_edges_object",
        "ix_entity_edges_predicate",
    } <= index_names


# --------------------------------------------------------------------------
# Constraint behaviour, on in-memory SQLite
# --------------------------------------------------------------------------


async def _sqlite_session(*tables):
    """An in-memory SQLite session over just the given tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    return engine, AsyncSession(engine, expire_on_commit=False)


def _edge(subject, predicate, obj, confidence=70):
    return EntityEdge(
        subject_type="story",
        subject_id=subject,
        predicate=predicate,
        object_type="story",
        object_id=obj,
        confidence=confidence,
    )


async def test_a_duplicate_five_tuple_raises_integrity_error():
    """The same edge twice is rejected, whatever confidence it was recorded at."""
    subject, obj = uuid.uuid4(), uuid.uuid4()
    engine, session = await _sqlite_session(EntityEdge.__table__)
    try:
        session.add(_edge(subject, EdgePredicate.SAME_EVENT_AS, obj, 70))
        await session.commit()

        session.add(_edge(subject, EdgePredicate.SAME_EVENT_AS, obj, 55))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        count = await session.execute(select(func.count()).select_from(EntityEdge))
        assert count.scalar_one() == 1, "the rejected row must not have landed"
    finally:
        await session.close()
        await engine.dispose()


async def test_a_different_predicate_is_not_a_duplicate():
    """Only the 5-tuple is unique: a different predicate is a different edge."""
    subject, obj = uuid.uuid4(), uuid.uuid4()
    engine, session = await _sqlite_session(EntityEdge.__table__)
    try:
        session.add(_edge(subject, EdgePredicate.SAME_EVENT_AS, obj, 70))
        session.add(_edge(subject, EdgePredicate.PART_OF_NARRATIVE, obj, 30))
        await session.commit()

        count = await session.execute(select(func.count()).select_from(EntityEdge))
        assert count.scalar_one() == 2
    finally:
        await session.close()
        await engine.dispose()


async def test_a_different_direction_is_not_a_duplicate():
    """subject->object and object->subject stay distinct, as the tuple has it."""
    subject, obj = uuid.uuid4(), uuid.uuid4()
    engine, session = await _sqlite_session(EntityEdge.__table__)
    try:
        session.add(_edge(subject, EdgePredicate.SAME_EVENT_AS, obj, 70))
        session.add(_edge(obj, EdgePredicate.SAME_EVENT_AS, subject, 70))
        await session.commit()

        count = await session.execute(select(func.count()).select_from(EntityEdge))
        assert count.scalar_one() == 2
    finally:
        await session.close()
        await engine.dispose()


# --------------------------------------------------------------------------
# The migration, against a real Postgres scratch schema
# --------------------------------------------------------------------------


def migration_sql(schema: str) -> str:
    """The migration with its `public.` qualifier aimed at a scratch schema."""
    body = MIGRATION.read_text().split("DO $$", 1)[1]
    return "DO $$" + body.replace("public.", f"{schema}.")


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


async def create_edges_table(conn: asyncpg.Connection) -> None:
    """A scratch entity_edges shaped like dev's, minus the reporting_units FK.

    The enum type is created here rather than reused so the scratch schema is
    self-contained and its CREATE does not depend on public.
    """
    await conn.execute(
        f"CREATE TYPE {SCHEMA}.edgepredicate AS ENUM "
        "('CORROBORATES','DISPUTES','SAME_EVENT_AS','PART_OF_NARRATIVE','CAUSED_BY')"
    )
    await conn.execute(
        f"CREATE TABLE {SCHEMA}.entity_edges ("
        "id serial PRIMARY KEY, "
        "subject_type varchar(20) NOT NULL, "
        "subject_id uuid NOT NULL, "
        f"predicate {SCHEMA}.edgepredicate NOT NULL, "
        "object_type varchar(20) NOT NULL, "
        "object_id uuid NOT NULL, "
        "confidence integer NOT NULL, "
        "source_unit_id uuid, "
        "created_at timestamptz NOT NULL)"
    )


async def insert_edge(conn, subject, obj, predicate=PREDICATE_LABEL, confidence=70):
    return await conn.fetchval(INSERT.format(s=SCHEMA), subject, predicate, obj, confidence)


async def unique_def(conn) -> str | None:
    return await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass($1) AND conname = 'uq_entity_edge'",
        f"{SCHEMA}.entity_edges",
    )


async def test_migration_adds_the_constraint(scratch):
    await create_edges_table(scratch)

    await scratch.execute(migration_sql(SCHEMA))

    assert await unique_def(scratch) == (
        "UNIQUE (subject_type, subject_id, predicate, object_type, object_id)"
    )


async def test_applying_the_migration_twice_changes_nothing(scratch):
    """Re-apply is a no-op: no error, and rows written in between survive."""
    await create_edges_table(scratch)
    subject, obj = uuid.uuid4(), uuid.uuid4()
    await scratch.execute(migration_sql(SCHEMA))
    await insert_edge(scratch, subject, obj)
    before = await unique_def(scratch)

    await scratch.execute(migration_sql(SCHEMA))

    assert await unique_def(scratch) == before
    assert await scratch.fetchval(f"SELECT count(*) FROM {SCHEMA}.entity_edges") == 1, (
        "the second apply must not dedupe a table the constraint already protects"
    )


async def test_migration_deletes_duplicates_before_constraining(scratch):
    """Duplicates are collapsed to min(id) inside the migration, then constrained.

    This is the case ADD CONSTRAINT alone would fail on: without the DELETE the
    migration could not be re-applied to a table that has picked up a duplicate,
    and an idempotent migration that can fail is not idempotent.
    """
    await create_edges_table(scratch)
    subject, other, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = await insert_edge(scratch, subject, other, confidence=70)
    duplicate = await insert_edge(scratch, subject, other, confidence=55)
    unrelated = await insert_edge(scratch, subject, third, confidence=60)
    assert duplicate > first

    await scratch.execute(migration_sql(SCHEMA))

    rows = await scratch.fetch(
        f"SELECT id, object_id, confidence FROM {SCHEMA}.entity_edges ORDER BY id"
    )
    assert len(rows) == 2, "the duplicate 5-tuple must be gone"
    surviving = {r["object_id"]: r for r in rows}
    assert surviving[other]["id"] == first, "the earliest row must be the one kept"
    assert surviving[other]["confidence"] == 70, "the kept row keeps its own confidence"
    assert surviving[third]["id"] == unrelated
    assert await unique_def(scratch) is not None


async def test_migration_leaves_an_already_constrained_table_alone(scratch):
    """A second, differently-shaped apply neither errors nor deletes a live row."""
    await create_edges_table(scratch)
    await scratch.execute(migration_sql(SCHEMA))
    subject, obj = uuid.uuid4(), uuid.uuid4()
    await insert_edge(scratch, subject, obj)

    await scratch.execute(migration_sql(SCHEMA))

    assert await scratch.fetchval(f"SELECT count(*) FROM {SCHEMA}.entity_edges") == 1
    assert await unique_def(scratch) == (
        "UNIQUE (subject_type, subject_id, predicate, object_type, object_id)"
    )


async def test_migration_skips_a_table_that_does_not_exist(scratch):
    """Nothing to constrain is not an error, so an early apply cannot fail."""
    await scratch.execute(migration_sql(SCHEMA))

    assert not await scratch.fetchval("SELECT to_regclass($1)", f"{SCHEMA}.entity_edges")


# --------------------------------------------------------------------------
# The narrative backfill, which matches on the same 5-tuple
# --------------------------------------------------------------------------


async def _story(day_offset: int, entities: list[str]):
    return Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=entities,
        status=Story.Status.QUEUED,
        created_at=datetime.now(timezone.utc) + timedelta(days=day_offset),
    )


async def test_backfill_creates_the_edge_when_it_is_absent(db_session):
    """Baseline for the skip test: with no edge present, the writer still writes."""
    target = await _story(0, ["Israel", "Hamas", "Gaza", "Netanyahu"])
    older = await _story(-10, ["Israel", "Hamas", "Gaza", "Netanyahu"])
    db_session.add_all([target, older])
    await db_session.commit()

    results = await link_narrative_arcs(db_session, target.id)

    assert len(results) == 1
    assert results[0]["created"] is True
    assert results[0]["predicate"] == "same_event_as"
    count = await db_session.execute(select(func.count()).select_from(EntityEdge))
    assert count.scalar_one() == 1


async def test_backfill_still_skips_an_edge_that_already_exists(db_session):
    """The 5-tuple check-then-insert still skips, now that the DB would refuse a dup.

    If the writer's SELECT matched on different columns than the constraint, the
    check would miss and the INSERT would raise IntegrityError here instead of
    quietly skipping. So this fails loudly if the two ever drift apart.
    """
    target = await _story(0, ["Israel", "Hamas", "Gaza", "Netanyahu"])
    older = await _story(-10, ["Israel", "Hamas", "Gaza", "Netanyahu"])
    db_session.add_all([target, older])
    await db_session.commit()
    existing = _edge(target.id, EdgePredicate.SAME_EVENT_AS, older.id, confidence=80)
    db_session.add(existing)
    await db_session.commit()

    results = await link_narrative_arcs(db_session, target.id)

    assert len(results) == 1
    assert results[0]["created"] is False
    assert results[0]["confidence"] == 80, "the existing row's confidence is reported"
    count = await db_session.execute(select(func.count()).select_from(EntityEdge))
    assert count.scalar_one() == 1, "no second edge was written"

    # Re-running the backfill changes nothing either.
    again = await link_narrative_arcs(db_session, target.id)
    assert [r["created"] for r in again] == [False]
    count = await db_session.execute(select(func.count()).select_from(EntityEdge))
    assert count.scalar_one() == 1