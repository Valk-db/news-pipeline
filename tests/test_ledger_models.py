"""Tests for the ledger table definitions: src/schema/models.py PipelineRun and DeadLetter,
plus raw_articles.terminal_state.

These are the contract the SQL migrations in supabase/migrations have to match, and the
contract scripts/check_orphans.py queries. Pure metadata checks for the shape, plus a round
trip through the real tables for the defaults, since a column can look right in metadata and
still come back wrong.
"""

import uuid
from datetime import datetime, UTC

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typing import AsyncGenerator

from src.schema.models import Base, DeadLetter, PipelineRun, RawArticle, SourceTier


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Use the test database session from conftest.py"""
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


def _column(model, name):
    return model.__table__.c[name]


def _index_names(model) -> set:
    return {index.name for index in model.__table__.indexes}


def _type_name(column) -> str:
    return str(column.type)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands datetimes back without a tzinfo, so compare in UTC either way."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class TestTablesExist:
    """Both new tables are in the metadata, and 20261001000000/200 create them."""

    def test_pipeline_runs_table_exists(self):
        assert "pipeline_runs" in Base.metadata.tables

    def test_dead_letters_table_exists(self):
        assert "dead_letters" in Base.metadata.tables


class TestPipelineRunColumns:
    """One row per stage per run, with the counters and the error."""

    def test_id_is_the_primary_key(self):
        pk = list(PipelineRun.__table__.primary_key.columns)
        assert [col.name for col in pk] == ["id"]
        assert _type_name(pk[0]) == "UUID"
        assert pk[0].default.arg is not None

    def test_run_id_groups_stages(self):
        col = _column(PipelineRun, "run_id")
        assert _type_name(col) == "UUID"
        assert col.nullable is False
        assert col.default is None  # The caller supplies it, there is no runs table to key off
        assert col.foreign_keys == set()

    def test_stage_is_a_short_text(self):
        col = _column(PipelineRun, "stage")
        assert _type_name(col) == "VARCHAR(50)"
        assert col.nullable is False

    def test_timestamps(self):
        started = _column(PipelineRun, "started_at")
        assert _type_name(started) == "DATETIME"
        assert started.nullable is False
        assert callable(started.default.arg)
        finished = _column(PipelineRun, "finished_at")
        assert _type_name(finished) == "DATETIME"
        assert finished.nullable is True  # NULL while the stage is running
        assert finished.default is None

    def test_counters_default_to_zero(self):
        for name in ("items_in", "items_out"):
            col = _column(PipelineRun, name)
            assert _type_name(col) == "INTEGER"
            assert col.nullable is False
            assert col.default.arg == 0

    def test_items_dropped_by_reason_is_a_json_map(self):
        col = _column(PipelineRun, "items_dropped_by_reason")
        assert _type_name(col) == "JSON"
        assert col.nullable is False
        # A callable default, so each row gets its own dict rather than a shared one.
        assert col.default.arg is not None
        first, second = col.default.arg(None), col.default.arg(None)
        assert first == {} and second == {} and first is not second

    def test_error_is_optional(self):
        col = _column(PipelineRun, "error")
        assert _type_name(col) == "TEXT"
        assert col.nullable is True

    def test_indexes_cover_run_id_stage_and_time(self):
        assert {"ix_pipeline_runs_run_id", "ix_pipeline_runs_stage", "ix_pipeline_runs_started_at"} <= _index_names(PipelineRun)

    async def test_round_trip_applies_the_defaults(self, db_session):
        run_id = uuid.uuid4()
        row = PipelineRun(id=uuid.uuid4(), run_id=run_id, stage="ingest_rss")
        db_session.add(row)
        await db_session.flush()

        stored = (
            await db_session.execute(select(PipelineRun).where(PipelineRun.run_id == run_id))
        ).scalar_one()
        assert stored.items_in == 0
        assert stored.items_out == 0
        assert stored.items_dropped_by_reason == {}
        assert stored.error is None
        assert stored.finished_at is None
        assert stored.started_at is not None

    async def test_stored_started_at_is_within_the_call(self, db_session):
        before = datetime.now(UTC)
        row = PipelineRun(id=uuid.uuid4(), run_id=uuid.uuid4(), stage="dedupe")
        db_session.add(row)
        await db_session.flush()
        after = datetime.now(UTC)
        assert before <= _as_utc(row.started_at) <= after


class TestDeadLetterColumns:
    """A dead letter links an article when there is one, and keeps the item when there is not."""

    def test_id_is_the_primary_key(self):
        pk = list(DeadLetter.__table__.primary_key.columns)
        assert [col.name for col in pk] == ["id"]
        assert _type_name(pk[0]) == "UUID"
        assert pk[0].default.arg is not None

    def test_run_id_is_optional(self):
        col = _column(DeadLetter, "run_id")
        assert _type_name(col) == "UUID"
        assert col.nullable is True

    def test_stage_and_reason_are_required(self):
        stage = _column(DeadLetter, "stage")
        assert _type_name(stage) == "VARCHAR(50)"
        assert stage.nullable is False
        reason = _column(DeadLetter, "reason")
        assert _type_name(reason) == "TEXT"
        assert reason.nullable is False

    def test_article_id_is_a_set_null_foreign_key(self):
        col = _column(DeadLetter, "article_id")
        assert _type_name(col) == "UUID"
        assert col.nullable is True
        assert len(col.foreign_keys) == 1
        fkey = next(iter(col.foreign_keys))
        assert fkey.target_fullname == "raw_articles.id"
        # Deleting the article keeps the letter, so the failure outlives the row it describes.
        assert fkey.ondelete == "SET NULL"

    def test_payload_is_optional_json(self):
        col = _column(DeadLetter, "payload")
        assert _type_name(col) == "JSON"
        assert col.nullable is True
        assert col.default is None

    def test_created_at_defaults_to_now(self):
        col = _column(DeadLetter, "created_at")
        assert _type_name(col) == "DATETIME"
        assert col.nullable is False
        assert callable(col.default.arg)

    def test_indexes(self):
        assert {
            "ix_dead_letters_run_id",
            "ix_dead_letters_article_id",
            "ix_dead_letters_stage",
            "ix_dead_letters_created_at",
        } <= _index_names(DeadLetter)

    async def test_round_trip_with_a_payload(self, db_session):
        row = DeadLetter(id=uuid.uuid4(), stage="ingest_rss", reason="parse_failed",
                         payload={"url": "https://apnews.com/a", "n": 1})
        db_session.add(row)
        await db_session.flush()
        stored = (
            await db_session.execute(select(DeadLetter).where(DeadLetter.id == row.id))
        ).scalar_one()
        assert stored.run_id is None
        assert stored.article_id is None
        assert stored.payload == {"url": "https://apnews.com/a", "n": 1}
        assert stored.created_at is not None


class TestRawArticleTerminalState:
    """The one column added to an existing table, defaulting to pending."""

    def test_column_definition(self):
        col = _column(RawArticle, "terminal_state")
        assert _type_name(col) == "TEXT"
        assert col.nullable is False
        assert col.default.arg == "pending"
        # The server default is what the migration backfills existing rows with.
        assert col.server_default.arg == "pending"

    def test_index_is_plain_not_unique(self):
        names = {index.name for index in RawArticle.__table__.indexes}
        assert "ix_raw_articles_terminal_state" in names
        index = next(i for i in RawArticle.__table__.indexes if i.name == "ix_raw_articles_terminal_state")
        assert index.unique is False
        assert [col.name for col in index.columns] == ["terminal_state"]

    def test_existing_indexes_survived(self):
        names = _index_names(RawArticle)
        assert {"ix_raw_articles_published_at", "ix_raw_articles_source_domain", "ix_raw_articles_url_hash"} <= names

    async def test_new_articles_default_to_pending(self, db_session):
        article = RawArticle(
            id=uuid.uuid4(),
            url="https://reuters.com/article/x",
            url_hash=uuid.uuid4().hex,
            title="Headline",
            body_text="Body",
            source_domain="reuters.com",
            source_tier=SourceTier.TIER1,
            fetched_at=datetime.now(UTC),
        )
        db_session.add(article)
        await db_session.flush()
        stored = (
            await db_session.execute(select(RawArticle).where(RawArticle.id == article.id))
        ).scalar_one()
        assert stored.terminal_state == "pending"

    @pytest.mark.parametrize(
        "state",
        ["pending", "dropped:parse_failed", "duplicate_of:0f000000", "unit:0f000001",
         "story:0f000002", "candidate"],
    )
    async def test_documented_values_fit_in_the_column(self, db_session, state):
        assert len(state) < 64
        article = RawArticle(
            id=uuid.uuid4(),
            url=f"https://bbc.com/{state}",
            url_hash=uuid.uuid4().hex,
            title="Headline",
            body_text="Body",
            source_domain="bbc.com",
            source_tier=SourceTier.TIER1,
            fetched_at=datetime.now(UTC),
            terminal_state=state,
        )
        db_session.add(article)
        await db_session.flush()
        stored = (
            await db_session.execute(select(RawArticle).where(RawArticle.id == article.id))
        ).scalar_one()
        assert stored.terminal_state == state
