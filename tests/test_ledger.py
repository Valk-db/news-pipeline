"""Tests for the pipeline ledger: src/shared/ledger.py.

Uses the shared SQLite session from conftest.py, so the real INSERTs and UPDATEs run against
the real pipeline_runs, dead_letters and raw_articles tables. The interesting failures here
are a ledger row that claims a stage finished when it did not, a drop reason that did not
accumulate, and an error that got swallowed instead of raised.
"""

import uuid
from datetime import datetime, timedelta, UTC
from typing import Any, AsyncGenerator, List

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.schema.models import DeadLetter, PipelineRun, RawArticle, SourceTier
from src.shared.ledger import (
    MAX_ERROR_CHARS,
    PENDING_TERMINAL_STATE,
    StageRecorder,
    duplicate_of_state,
    dropped_state,
    record_dead_letter,
    set_terminal_state,
    stage_run,
    story_state,
    unit_state,
)

RUN_ID = uuid.uuid4()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Use the test database session from conftest.py"""
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


def _as_utc(value: datetime) -> datetime:
    """SQLite hands datetimes back without a tzinfo, so compare in UTC either way."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _make_article(session: AsyncSession, terminal_state: Any = None) -> RawArticle:
    """One ingested article. terminal_state=None leaves the column to its own default."""
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://apnews.com/article/{uuid.uuid4().hex[:8]}",
        url_hash=uuid.uuid4().hex,
        title="Council approves transit plan",
        body_text="Body text long enough to be plausible. " * 8,
        source_domain="apnews.com",
        source_tier=SourceTier.TIER1,
        fetched_at=datetime.now(UTC),
    )
    if terminal_state is not None:
        article.terminal_state = terminal_state
    session.add(article)
    await session.flush()
    return article


async def _runs_for(session: AsyncSession, run_id: uuid.UUID) -> List[PipelineRun]:
    stmt = select(PipelineRun).where(PipelineRun.run_id == run_id).order_by(PipelineRun.stage)
    return list((await session.execute(stmt)).scalars().all())


async def _letters(session: AsyncSession) -> List[DeadLetter]:
    return list((await session.execute(select(DeadLetter))).scalars().all())


class TestStageRunSuccess:
    """The clean exit path writes the counts the stage reported."""

    async def test_writes_counts_and_finished_at(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "ingest_rss") as stage:
            stage.count_in(10).count_out(7).drop("parse_failed", 3)

        rows = await _runs_for(db_session, run_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.stage == "ingest_rss"
        assert row.run_id == run_id
        assert row.items_in == 10
        assert row.items_out == 7
        assert row.items_dropped_by_reason == {"parse_failed": 3}
        assert row.error is None
        assert row.started_at is not None
        assert row.finished_at is not None
        assert _as_utc(row.finished_at) >= _as_utc(row.started_at)

    async def test_counters_accumulate_across_calls(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "cluster") as stage:
            stage.count_in(2)
            stage.count_in(3)
            stage.count_out(1)
            stage.count_out(4)
        row = (await _runs_for(db_session, run_id))[0]
        assert row.items_in == 5
        assert row.items_out == 5

    async def test_drop_reasons_accumulate(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "geocode") as stage:
            stage.drop("geocode_empty")
            stage.drop("geocode_empty")
            stage.drop("parse_failed", 4)
        row = (await _runs_for(db_session, run_id))[0]
        assert row.items_dropped_by_reason == {"geocode_empty": 2, "parse_failed": 4}

    async def test_dropped_total_sums_reasons(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "dedupe") as stage:
            stage.drop("url_duplicate", 2).drop("content_duplicate", 3)
            assert stage.dropped_total() == 5
        row = (await _runs_for(db_session, run_id))[0]
        assert sum(row.items_dropped_by_reason.values()) == 5

    async def test_unreported_stage_records_zeros(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "noop"):
            pass
        row = (await _runs_for(db_session, run_id))[0]
        assert (row.items_in, row.items_out) == (0, 0)
        assert row.items_dropped_by_reason == {}
        assert row.finished_at is not None

    async def test_row_exists_while_the_stage_runs(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "dedupe") as stage:
            rows = await _runs_for(db_session, run_id)
            assert len(rows) == 1
            assert rows[0].finished_at is None
            assert stage.run_id == run_id
            assert stage.stage_name == "dedupe"
        assert (await _runs_for(db_session, run_id))[0].finished_at is not None

    async def test_one_row_per_stage_sharing_a_run_id(self, db_session):
        run_id = uuid.uuid4()
        for stage_name in ("ingest_rss", "dedupe", "cluster", "geocode"):
            async with stage_run(db_session, run_id, stage_name) as stage:
                stage.count_in(4).count_out(4)
        rows = await _runs_for(db_session, run_id)
        assert [row.stage for row in rows] == ["cluster", "dedupe", "geocode", "ingest_rss"]
        assert len({row.id for row in rows}) == 4

    async def test_missing_run_id_starts_a_fresh_one(self, db_session):
        async with stage_run(db_session, None, "ingest_rss") as stage:
            assert isinstance(stage.run_id, uuid.UUID)
        rows = await _runs_for(db_session, stage.run_id)
        assert len(rows) == 1 and rows[0].stage == "ingest_rss"

    async def test_repr_is_readable(self, db_session):
        async with stage_run(db_session, RUN_ID, "dedupe") as stage:
            stage.count_in(1).drop("url_duplicate")
            text = repr(stage)
        assert "dedupe" in text and "url_duplicate" in text


class TestStageRunFailure:
    """The exception path writes the error and still raises."""

    async def test_writes_error_and_reraises(self, db_session):
        run_id = uuid.uuid4()
        with pytest.raises(RuntimeError, match="upstream feed timed out"):
            async with stage_run(db_session, run_id, "ingest_rss") as stage:
                stage.count_in(5).count_out(2)
                raise RuntimeError("upstream feed timed out")

        row = (await _runs_for(db_session, run_id))[0]
        assert row.error is not None
        assert "upstream feed timed out" in row.error
        assert "RuntimeError" in row.error
        # The counts the stage did report survive, so a partial run is still legible.
        assert row.items_in == 5
        assert row.items_out == 2
        assert row.finished_at is not None

    async def test_error_row_is_written_before_the_raise_propagates(self, db_session):
        run_id = uuid.uuid4()
        captured = None
        try:
            async with stage_run(db_session, run_id, "geocode"):
                raise ValueError("geocoder returned nothing")
        except ValueError as exc:
            captured = str(exc)
            # Read inside the except block, before anything unwinds further.
            row = (await _runs_for(db_session, run_id))[0]
            assert "geocoder returned nothing" in row.error
        assert captured == "geocoder returned nothing"

    async def test_error_text_is_clipped(self, db_session):
        run_id = uuid.uuid4()
        with pytest.raises(RuntimeError):
            async with stage_run(db_session, run_id, "ingest_rss"):
                raise RuntimeError("x" * (MAX_ERROR_CHARS * 3))
        row = (await _runs_for(db_session, run_id))[0]
        assert len(row.error) <= MAX_ERROR_CHARS + len(" ... truncated")
        assert row.error.endswith("truncated")

    async def test_fail_records_a_handled_error_without_raising(self, db_session):
        run_id = uuid.uuid4()
        async with stage_run(db_session, run_id, "cluster") as stage:
            stage.count_in(3).count_out(3)
            await stage.fail("clustering degraded, continued anyway")
        row = (await _runs_for(db_session, run_id))[0]
        assert row.error == "clustering degraded, continued anyway"
        assert row.items_in == 3
        assert row.finished_at is not None

    async def test_failing_stage_does_not_stop_the_next_one(self, db_session):
        run_id = uuid.uuid4()
        with pytest.raises(RuntimeError):
            async with stage_run(db_session, run_id, "dedupe"):
                raise RuntimeError("boom")
        async with stage_run(db_session, run_id, "cluster") as stage:
            stage.count_in(2)
        rows = {row.stage: row for row in await _runs_for(db_session, run_id)}
        assert rows["dedupe"].error is not None
        assert rows["cluster"].error is None
        assert rows["cluster"].items_in == 2


class TestRecordDeadLetter:
    """Dead letters record both shapes, the linked article and the loose payload."""

    async def test_records_an_article_backed_letter(self, db_session):
        article = await _make_article(db_session)
        run_id = uuid.uuid4()
        row = await record_dead_letter(
            db_session, "geocode", "geocode_empty", article_id=article.id, run_id=run_id
        )
        assert row.id is not None
        assert row.payload is None

        stored = (await _letters(db_session))[0]
        assert stored.article_id == article.id
        assert stored.stage == "geocode"
        assert stored.reason == "geocode_empty"
        assert stored.run_id == run_id
        assert stored.created_at is not None

    async def test_records_a_payload_only_letter(self, db_session):
        payload = {"url": "https://apnews.com/article/x", "title": "no body in the feed"}
        row = await record_dead_letter(
            db_session, "ingest_rss", "parse_failed", payload=payload, run_id=uuid.uuid4()
        )
        assert row.article_id is None
        stored = (await _letters(db_session))[0]
        assert stored.payload == payload
        assert stored.article_id is None

    async def test_records_a_bare_failure(self, db_session):
        await record_dead_letter(db_session, "ingest_rss", "rate_limited")
        stored = (await _letters(db_session))[0]
        assert stored.run_id is None
        assert stored.article_id is None
        assert stored.payload is None
        assert stored.reason == "rate_limited"

    async def test_created_at_is_utc(self, db_session):
        before = datetime.now(UTC) - timedelta(seconds=1)
        await record_dead_letter(db_session, "ingest_rss", "parse_failed")
        stored = (await _letters(db_session))[0]
        after = datetime.now(UTC) + timedelta(seconds=1)
        naive = stored.created_at.replace(tzinfo=UTC)
        assert before <= naive <= after

    async def test_many_letters_keep_their_own_ids(self, db_session):
        ids = {
            (await record_dead_letter(db_session, "ingest_rss", f"reason_{n}")).id
            for n in range(3)
        }
        assert len(ids) == 3
        assert len(await _letters(db_session)) == 3


class TestSetTerminalState:
    """Terminal state moves an article forward and reports whether a row existed."""

    async def test_moves_a_pending_article(self, db_session):
        article = await _make_article(db_session)
        assert article.terminal_state == PENDING_TERMINAL_STATE

        assert await set_terminal_state(db_session, article.id, "candidate") is True

        refreshed = (
            await db_session.execute(select(RawArticle).where(RawArticle.id == article.id))
        ).scalar_one()
        assert refreshed.terminal_state == "candidate"

    async def test_unknown_article_returns_false(self, db_session):
        assert await set_terminal_state(db_session, uuid.uuid4(), "candidate") is False

    @pytest.mark.parametrize(
        "state",
        [
            "pending",
            "dropped:parse_failed",
            f"duplicate_of:{uuid.UUID(int=1)}",
            f"unit:{uuid.UUID(int=2)}",
            f"story:{uuid.UUID(int=3)}",
            "candidate",
        ],
    )
    async def test_every_documented_state_is_storable(self, db_session, state):
        article = await _make_article(db_session)
        assert await set_terminal_state(db_session, article.id, state) is True
        refreshed = (
            await db_session.execute(select(RawArticle).where(RawArticle.id == article.id))
        ).scalar_one()
        assert refreshed.terminal_state == state

    async def test_state_builders_use_the_documented_spelling(self):
        assert dropped_state("parse_failed") == "dropped:parse_failed"
        assert duplicate_of_state(uuid.UUID(int=7)) == f"duplicate_of:{uuid.UUID(int=7)}"
        assert unit_state(uuid.UUID(int=8)) == f"unit:{uuid.UUID(int=8)}"
        assert story_state(uuid.UUID(int=9)) == f"story:{uuid.UUID(int=9)}"

    async def test_terminal_state_and_dead_letter_travel_together(self, db_session):
        article = await _make_article(db_session)
        await set_terminal_state(db_session, article.id, dropped_state("parse_failed"))
        await record_dead_letter(
            db_session, "ingest_rss", "parse_failed", article_id=article.id
        )
        assert len(await _letters(db_session)) == 1
        refreshed = (
            await db_session.execute(select(RawArticle).where(RawArticle.id == article.id))
        ).scalar_one()
        assert refreshed.terminal_state == "dropped:parse_failed"


class TestRecorderCounters:
    """The recorder counts without writing, so a stage can count in a tight loop."""

    async def test_counters_work_on_an_unattached_row(self, db_session):
        # A row that is never added to the session: the counting methods never touch it.
        recorder = StageRecorder(db_session, PipelineRun(run_id=RUN_ID, stage="dedupe"))
        recorder.count_in(3).count_out(1).drop("parse_failed", 2)
        assert (recorder.items_in, recorder.items_out) == (3, 1)
        assert recorder.dropped == {"parse_failed": 2}
        assert recorder.dropped_total() == 2
        assert recorder.run_id == RUN_ID
        assert recorder.stage_name == "dedupe"
        assert recorder.error is None
        assert recorder._row.finished_at is None
        assert await _runs_for(db_session, RUN_ID) == []
