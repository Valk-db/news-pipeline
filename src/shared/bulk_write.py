"""Chunked, retryable writes for batches of ORM rows.

SQLAlchemy's unit of work turns every pending object of one mapper into a
single multi-row INSERT at flush time, so "add N rows, commit once" is one
statement spanning the whole batch: one dropped connection and every row is
lost, even though nothing was wrong with the data. That is how a 997-article
ingest died tonight on row 991 with ``InterfaceError: connection is closed``.
Chunking caps the blast radius at one statement and lets a retry salvage the
batch instead of the run.

Only a dead connection is retried. An IntegrityError is a permanent statement
about the rows themselves, so it propagates on the first attempt -- retrying it
would only fail the same way three times, more slowly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 150
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)

# asyncpg and libpq each have their own way of saying "the socket died, the
# rows are fine". Matched on text so this module needs neither driver imported.
_CONNECTION_GONE = (
    "connection is closed",
    "connection was closed",
    "connection was closed in the middle",
    "connection is already closed",
    "server closed the connection unexpectedly",
    "terminating connection",
    "the connection is lost",
    "cannot operate on a closed database",
)


class SupportsBulkWrite(Protocol):
    """The slice of AsyncSession this module uses."""

    def add_all(self, objects: Sequence[Any]) -> None: ...
    async def flush(self) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


def _says_connection_died(exc: BaseException) -> bool:
    """True if any error in the chain reads like a dead connection."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = str(exc).lower()
        if any(phrase in text for phrase in _CONNECTION_GONE):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def is_transient(exc: BaseException) -> bool:
    """True only for "the connection died", never for "these rows are wrong".

    IntegrityError is excluded first: it is a DBAPIError subclass and would
    otherwise match the type checks below.
    """
    if isinstance(exc, IntegrityError):
        return False
    if isinstance(exc, (InterfaceError, OperationalError)):
        return True
    if isinstance(exc, DBAPIError):
        return _says_connection_died(exc)
    return False


async def bulk_write(
    session: SupportsBulkWrite,
    objects: Sequence[Any],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_attempts: int = MAX_ATTEMPTS,
    backoff: Sequence[float] = BACKOFF_SECONDS,
) -> int:
    """Add, flush and commit objects in chunks, retrying a dead connection.

    Each chunk is one INSERT of at most ``chunk_size`` rows followed by its own
    commit, so a failure costs one chunk rather than the batch. On a transient
    connection error the session is rolled back, the chunk is re-added (rollback
    expunges the objects that were still pending) and the attempt is repeated
    after a backoff. Anything else -- an IntegrityError, a constraint violation,
    a bad statement -- is raised immediately.

    The session must hold no other unflushed work: a retry rolls back the whole
    transaction, so pending objects that are not part of ``objects`` would be
    lost with it.

    Returns the number of objects written.
    """
    rows = list(objects)
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        for attempt in range(1, max_attempts + 1):
            try:
                session.add_all(chunk)
                await session.flush()
                await session.commit()
                break
            except Exception as exc:
                if not is_transient(exc):
                    raise
                await session.rollback()
                if attempt == max_attempts:
                    logger.error(
                        "bulk write: chunk of %d rows failed %d times on a dead "
                        "connection (%s); giving up", len(chunk), attempt, exc,
                    )
                    raise
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                logger.warning(
                    "bulk write: transient connection error on chunk of %d rows "
                    "(%s); retry %d/%d in %.1fs",
                    len(chunk), exc, attempt, max_attempts - 1, delay,
                )
                await asyncio.sleep(delay)
    return len(rows)