"""Tests for the chunked bulk writer: src/shared/bulk_write.py.

The session is a fake rather than a real one on purpose. What is being tested
is the retry policy -- how many times a chunk is attempted, what is retried and
what is not, and what ends up written -- and a real database cannot be made to
drop a connection on the second INSERT of a chunk on demand.
"""

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from src.shared.bulk_write import DEFAULT_CHUNK_SIZE, bulk_write, is_transient


def dead_connection(msg: str = "connection is closed") -> InterfaceError:
    """The error asyncpg raises when the socket went away mid-statement."""
    return InterfaceError(
        "INSERT INTO raw_articles ...", {}, Exception(msg), connection_invalidated=True
    )


class Row:
    def __init__(self, n: int) -> None:
        self.n = n


class FakeSession:
    """Records the statements a batch produced, chunk by chunk.

    ``failures`` maps a chunk number to the exceptions that chunk's flush raises,
    in order. A re-add of a chunk already seen is counted as another attempt
    rather than a new chunk, which is exactly what the helper does on a retry.
    """

    def __init__(self, failures: dict[int, list[BaseException]] | None = None) -> None:
        self._failures = {n: list(errs) for n, errs in (failures or {}).items()}
        self.chunks: list[list[int]] = []
        self.attempts: list[int] = []
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0
        self.written: list[int] = []

    def add_all(self, objects) -> None:
        ids = [id(obj) for obj in objects]
        if not self.chunks or self.chunks[-1] != ids:
            self.chunks.append(ids)
            self.attempts.append(0)
        self.attempts[-1] += 1

    async def flush(self) -> None:
        self.flushes += 1
        pending = self._failures.get(len(self.chunks) - 1)
        if pending:
            raise pending.pop(0)

    async def commit(self) -> None:
        self.commits += 1
        self.written.extend(self.chunks[-1])

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff delays instead of sleeping through them."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("src.shared.bulk_write.asyncio.sleep", fake_sleep)
    return delays


async def test_transient_failure_retries_only_the_failing_chunk(no_sleep):
    rows = [Row(n) for n in range(10)]
    session = FakeSession({1: [dead_connection(), dead_connection()]})

    written = await bulk_write(session, rows, chunk_size=4, backoff=(0, 0))

    # Every row written, in order, exactly once.
    assert written == 10
    assert session.written == [id(row) for row in rows]
    # 4 + 4 + 2 rows: three chunks, three commits, one flush each plus retries.
    assert [len(c) for c in session.chunks] == [4, 4, 2]
    assert session.commits == 3
    # The healthy chunks were written once; the dead one took three attempts.
    assert session.attempts == [1, 3, 1]
    assert session.rollbacks == 2
    assert no_sleep == [0, 0]


async def test_backoff_doubles_then_stops(no_sleep):
    rows = [Row(0)]
    session = FakeSession({0: [dead_connection()] * 4})

    with pytest.raises(InterfaceError):
        await bulk_write(session, rows)

    # Default policy: 1s before the second attempt, 2s before the third.
    assert no_sleep == [1.0, 2.0]
    assert session.attempts == [3]
    assert session.written == []


async def test_chunk_size_is_the_default_and_is_respected():
    rows = [Row(n) for n in range(5)]
    session = FakeSession()
    await bulk_write(session, rows)
    assert [len(c) for c in session.chunks] == [min(len(rows), DEFAULT_CHUNK_SIZE)]
    assert session.commits == 1

    session = FakeSession()
    await bulk_write(session, rows, chunk_size=2)
    assert [len(c) for c in session.chunks] == [2, 2, 1]
    assert session.written == [id(row) for row in rows]


async def test_integrity_error_is_never_retried(no_sleep):
    rows = [Row(n) for n in range(4)]
    dup = IntegrityError(
        "INSERT INTO raw_articles ...",
        {},
        Exception('duplicate key value violates unique constraint "raw_articles_url_hash_key"'),
    )
    session = FakeSession({0: [dup]})

    with pytest.raises(IntegrityError):
        await bulk_write(session, rows, chunk_size=2, backoff=(0,))

    assert session.attempts == [1]
    assert session.commits == 0
    assert session.rollbacks == 0
    assert no_sleep == []


async def test_dead_connection_past_max_attempts_reraises(no_sleep):
    rows = [Row(n) for n in range(3)]
    session = FakeSession({0: [dead_connection()] * 5})

    with pytest.raises(InterfaceError):
        await bulk_write(session, rows, backoff=(0, 0))

    assert session.attempts == [3]
    assert session.rollbacks == 3
    assert session.written == []


async def test_earlier_chunks_survive_a_later_hard_failure(no_sleep):
    rows = [Row(n) for n in range(6)]
    session = FakeSession({2: [IntegrityError("INSERT", {}, Exception("not null"))]})

    with pytest.raises(IntegrityError):
        await bulk_write(session, rows, chunk_size=2, backoff=(0,))

    # Chunks 0 and 1 are committed; the bad chunk wrote nothing and was not retried.
    assert session.written == [id(row) for row in rows[:4]]
    assert session.attempts == [1, 1, 1]


async def test_non_database_errors_are_not_retried(no_sleep):
    rows = [Row(0)]
    session = FakeSession({0: [ValueError("bad row")]})
    with pytest.raises(ValueError):
        await bulk_write(session, rows, backoff=(0,))
    assert session.attempts == [1]
    assert no_sleep == []


async def test_empty_batch_writes_nothing():
    session = FakeSession()
    assert await bulk_write(session, []) == 0
    assert session.commits == 0


@pytest.mark.parametrize(
    "exc, expected",
    [
        (dead_connection(), True),
        (dead_connection("server closed the connection unexpectedly"), True),
        (dead_connection("terminating connection due to administrator command"), True),
        (OperationalError("COMMIT", {}, Exception("could not serialize")), True),
        (DBAPIError("INSERT", {}, Exception("server closed the connection unexpectedly")), True),
        (
            IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint")),
            False,
        ),
        (IntegrityError("INSERT", {}, Exception("connection is closed")), False),
        (DBAPIError("INSERT", {}, Exception('relation "raw_articles" does not exist')), False),
        (ValueError("connection is closed"), False),
    ],
)
def test_transient_classification(exc, expected):
    assert is_transient(exc) is expected