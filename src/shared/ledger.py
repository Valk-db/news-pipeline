"""Pipeline ledger: one row per stage per run, and the trail of items that never made it.

The pipeline is a chain of stages (ingest_rss, dedupe, cluster, geocode). Today a stage that
loses half its input, or an article that gets dropped and never mentioned again, leaves no
trace at all: the next stage simply sees a smaller list. This module is the bookkeeping that
makes those two cases visible, and it is deliberately small and dependency light (SQLAlchemy
and the standard library only) so any stage can adopt it without inheriting a framework.

Three things live here, one per table:

* stage_run is an async context manager wrapping one stage. It inserts a pipeline_runs row
  on entry, so the row exists while the stage runs (an interrupted stage is a row with
  finished_at still NULL), and it closes that row on exit with the counts the stage reported
  or with the error text if the stage raised.
* record_dead_letter writes one dead_letters row for an item the stage could not process.
* set_terminal_state moves an article's raw_articles.terminal_state forward, which is how
  "this article was dropped on purpose" becomes different from "this article vanished".

Two deliberate properties. First, these are ordinary writes in the caller's transaction, so
a stage that rolls back takes its ledger row with it. A rolled back stage must not leave a
row claiming it finished. Second, the counters are declared by the stage rather than measured
by the ledger, because measuring them would mean the ledger knows what each stage does; a
stage that reports nothing records zeros, and zeros are the truth until a stage says
otherwise.

How a stage calls it::

    async def ingest_rss(session: AsyncSession, run_id: UUID) -> None:
        entries = await fetch_feed_entries()
        async with stage_run(session, run_id, "ingest_rss") as stage:
            stage.count_in(len(entries))
            for entry in entries:
                try:
                    article = await store_article(session, entry)
                except ParseError:
                    stage.drop("parse_failed")
                    await record_dead_letter(
                        session,
                        stage="ingest_rss",
                        reason="parse_failed",
                        payload=dict(entry),      # no article row exists yet
                        run_id=run_id,
                    )
                    continue
                stage.count_out()
                await set_terminal_state(session, article.id, "pending")

    # Later stages mark where the article ended up, which is what check_orphans.py reads.
    await set_terminal_state(session, article.id, duplicate_of_state(kept.id))
    await set_terminal_state(session, article.id, unit_state(unit.id))

    # An item that cannot be processed after it became a row keeps a link instead of a copy.
    await record_dead_letter(session, "geocode", "geocode_empty", article_id=article.id)

Nothing here is wired into src/ingestion/run.py. A stage adopts the ledger by importing it and
opening the context manager, and a stage that does not is unaffected.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from typing import Any, AsyncGenerator, Dict, Optional, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import DeadLetter, PipelineRun, RawArticle

logger = logging.getLogger(__name__)

# The default terminal state, also the migration default. Exported so a caller writing a
# literal does not have to remember the spelling.
PENDING_TERMINAL_STATE = "pending"

# How much of an exception message is kept on pipeline_runs.error. A traceback helps nobody
# reading a ledger, and this table grows by one row per stage per run.
MAX_ERROR_CHARS = 2000


def _utcnow() -> datetime:
    """Now, in UTC. Every timestamp this module writes is UTC, aware."""
    return datetime.now(UTC)


def dropped_state(reason: str) -> str:
    """Build the terminal state for a deliberately discarded article, dropped:<reason>."""
    return f"dropped:{reason}"


def duplicate_of_state(article_id: UUID) -> str:
    """Build the terminal state for an article deduped into another, duplicate_of:<uuid>."""
    return f"duplicate_of:{article_id}"


def unit_state(unit_id: UUID) -> str:
    """Build the terminal state for an article that reached a reporting unit, unit:<uuid>."""
    return f"unit:{unit_id}"


def story_state(story_id: UUID) -> str:
    """Build the terminal state for an article that reached a story, story:<uuid>."""
    return f"story:{story_id}"


class StageRecorder:
    """The counters one stage reports, and the pipeline_runs row they get written into.

    Created by stage_run, which owns the transaction. The counting methods are synchronous
    and never touch the database, so a stage can count cheaply in a tight loop and the row is
    written once, on exit. fail is the exception: it writes immediately, because it exists for
    the stage that catches an error, records it, and carries on, and that record should not
    wait for an exit that may never come.
    """

    def __init__(self, session: AsyncSession, row: PipelineRun) -> None:
        self._session = session
        # Held as Any for writing: SQLAlchemy's declarative stubs type a mapped attribute as
        # the Column, so a plain assignment to it, which is the supported way to change a row,
        # reads as a type error. Nothing here needs the mapped type back.
        self._row: Any = row
        self.items_in = 0
        self.items_out = 0
        self.dropped: Dict[str, int] = {}
        self.error: Optional[str] = None
        self.run_id = row.run_id
        self.stage_name = row.stage

    def count_in(self, n: int = 1) -> "StageRecorder":
        """Record n items entering the stage. Returns self, so calls can chain."""
        self.items_in += int(n)
        return self

    def count_out(self, n: int = 1) -> "StageRecorder":
        """Record n items leaving the stage. Returns self, so calls can chain."""
        self.items_out += int(n)
        return self

    def drop(self, reason: str, n: int = 1) -> "StageRecorder":
        """Record n items discarded for reason. Repeats of the same reason accumulate."""
        self.dropped[reason] = self.dropped.get(reason, 0) + int(n)
        return self

    def dropped_total(self) -> int:
        """How many items this stage dropped, across all reasons."""
        return sum(self.dropped.values())

    async def fail(self, error_text: str) -> None:
        """Write error_text to the stage row now and flush it.

        For a stage that handles its own error and keeps going. The exit path in stage_run
        writes the error of an unhandled exception on its own, so a stage that does not catch
        anything never has to call this.
        """
        self.error = _clip_error(error_text)
        self._row.error = self.error
        await self._session.flush()

    async def _close(self, error_text: Optional[str] = None) -> None:
        """Stamp finished_at and the reported counters onto the row, then flush."""
        self._row.finished_at = _utcnow()
        self._row.items_in = self.items_in
        self._row.items_out = self.items_out
        # A copy, so a later drop() cannot mutate a column that has already been written.
        self._row.items_dropped_by_reason = dict(self.dropped)
        if error_text is not None:
            self.error = _clip_error(error_text)
            self._row.error = self.error
        await self._session.flush()

    def __repr__(self) -> str:
        return (
            f"StageRecorder(stage={self.stage_name!r}, run_id={self.run_id}, "
            f"in={self.items_in}, out={self.items_out}, dropped={self.dropped})"
        )


def _clip_error(error_text: str) -> str:
    """Trim an error message to MAX_ERROR_CHARS so one traceback cannot bloat the ledger."""
    text = str(error_text).strip()
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return text[:MAX_ERROR_CHARS] + " ... truncated"


@asynccontextmanager
async def stage_run(
    session: AsyncSession,
    run_id: Optional[UUID] = None,
    stage_name: str = "unknown",
) -> AsyncGenerator[StageRecorder, None]:
    """Time one pipeline stage and record what it did, in pipeline_runs.

    Args:
        session: the caller's async session. The row is written in the caller's transaction,
            so it commits and rolls back with the stage's own work.
        run_id: groups the stages of one execution. None starts a fresh run id, which is
            readable afterwards as recorder.run_id.
        stage_name: which stage this is, e.g. ingest_rss, dedupe, cluster, geocode.

    Yields:
        A StageRecorder. Call count_in, count_out and drop while the stage works. On a clean
        exit the row is closed with finished_at, items_in, items_out and
        items_dropped_by_reason. On an exception the same fields are written together with
        the error text, and the exception is raised again so the caller still sees it.

    An unhandled database error leaves the session unable to accept any write, so the error
    row is attempted and, if that attempt fails, logged and skipped. The original exception is
    raised either way: losing the ledger row matters less than losing the reason.
    """
    row = PipelineRun(
        id=uuid4(),
        run_id=run_id if run_id is not None else uuid4(),
        stage=stage_name,
        started_at=_utcnow(),
        items_in=0,
        items_out=0,
        items_dropped_by_reason={},
    )
    session.add(row)
    await session.flush()

    recorder = StageRecorder(session, row)
    try:
        yield recorder
    except Exception as exc:
        try:
            await recorder._close(error_text=f"{type(exc).__name__}: {exc}")
        except Exception as close_exc:
            # The stage error is the one that matters, so this is logged and the original
            # exception still propagates. A dead letter is better than a lost reason.
            logger.warning("ledger: could not record failure of stage %s: %s", stage_name, close_exc)
        raise
    await recorder._close()


async def record_dead_letter(
    session: AsyncSession,
    stage: str,
    reason: str,
    article_id: Optional[UUID] = None,
    payload: Optional[Dict[str, Any]] = None,
    run_id: Optional[UUID] = None,
) -> DeadLetter:
    """Record one item the pipeline could not process, in dead_letters.

    Args:
        session: the caller's async session.
        stage: the stage that gave up on the item.
        reason: a machine readable label, e.g. parse_failed, geocode_empty, low_confidence.
        article_id: the article row, when the item became one.
        payload: the item itself, when it never did (a raw feed entry, an API response).
        run_id: the execution that produced it, when the caller is inside stage_run and knows
            it, or None.

    Returns:
        The DeadLetter row, already flushed so its id is available to the caller.

    One or the other of article_id and payload is normally set, and the row also allows
    neither, which records a bare failure. Pair this with set_terminal_state when the article
    exists: the letter says the stage failed on it, the terminal state says the pipeline
    stopped there on purpose.
    """
    row = DeadLetter(
        id=uuid4(),
        run_id=run_id,
        stage=stage,
        reason=reason,
        article_id=article_id,
        payload=payload,
        created_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def set_terminal_state(
    session: AsyncSession,
    article_id: UUID,
    state: str,
) -> bool:
    """Move one article's raw_articles.terminal_state to state.

    Args:
        session: the caller's async session.
        article_id: the article to move.
        state: one of the documented values: pending, dropped:<reason>, duplicate_of:<uuid>,
            unit:<uuid>, story:<uuid>, candidate. The builders dropped_state,
            duplicate_of_state, unit_state and story_state produce the parameterised ones, so
            a typo cannot become a state nothing queries.

    Returns:
        True if a row was updated, False if no article has that id. The value is not
        validated here, deliberately: a new stage may need a new state, and rejecting it at
        write time would lose the fact rather than record it.
    """
    # A bulk update so the row is not loaded and no article is pulled into the session.
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(RawArticle).where(RawArticle.id == article_id).values(terminal_state=state)
        ),
    )
    await session.flush()
    return bool(result.rowcount)


async def get_stage_runs(session: AsyncSession, run_id: UUID) -> list[PipelineRun]:
    """Every pipeline_runs row for one execution, oldest stage start first.

    Small convenience for callers and scripts that want to see how a single run went.
    """
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.run_id == run_id)
        .order_by(PipelineRun.started_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
