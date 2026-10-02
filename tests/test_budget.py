"""Tests for the shared budget counter: src/shared/budget.py.

The statement under test runs against SQLite (aiosqlite, a dev dependency) rather than
Postgres, because its whole point is that it is one portable statement -- an
INSERT .. ON CONFLICT DO UPDATE .. WHERE .. RETURNING that both dialects run -- and the
tests here are about the semantics the callers rely on:

  * the cap holds across separate spend() calls, i.e. across processes
  * a refused spend costs nothing
  * a new day starts a new budget
  * an unreachable counter refuses to spend (fail safe), it does not allow

No Postgres required. The dev-database cross-process proof is the live check, not a unit test.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared import budget as budget_module
from src.shared.budget import MYMEMORY_CHARS, spend, spend_sync, used

TODAY = date(2026, 10, 2)


# The engine comes from the autouse budget_counter fixture in conftest.py: one
# file-backed SQLite counter per test, empty. A file rather than :memory: because
# spend_sync runs each call in its own event loop, and an in-memory database would be a
# different database per loop.


@pytest.fixture
def counter(budget_counter):
    """The counter this test spends against, so the fixtures below read as 'the counter'."""
    return budget_counter


class TestSpend:
    async def test_first_spend_creates_the_row(self, counter):
        assert await spend(MYMEMORY_CHARS, 100, 500, day=TODAY) == 100
        assert await used(MYMEMORY_CHARS, day=TODAY) == 100

    async def test_spends_accumulate(self, counter):
        assert await spend(MYMEMORY_CHARS, 100, 500, day=TODAY) == 100
        assert await spend(MYMEMORY_CHARS, 50, 500, day=TODAY) == 150

    async def test_two_callers_share_one_cap(self, counter):
        """The point of the table: a second process sees the first one's spend."""
        assert await spend(MYMEMORY_CHARS, 300, 400, day=TODAY) == 300
        assert await spend(MYMEMORY_CHARS, 150, 400, day=TODAY) is None
        assert await used(MYMEMORY_CHARS, day=TODAY) == 300

    async def test_spend_exactly_at_the_cap_is_allowed(self, counter):
        assert await spend(MYMEMORY_CHARS, 500, 500, day=TODAY) == 500
        assert await spend(MYMEMORY_CHARS, 1, 500, day=TODAY) is None

    async def test_refused_spend_changes_nothing(self, counter):
        await spend(MYMEMORY_CHARS, 500, 500, day=TODAY)
        assert await spend(MYMEMORY_CHARS, 1, 500, day=TODAY) is None
        assert await used(MYMEMORY_CHARS, day=TODAY) == 500

    async def test_a_new_day_is_a_new_budget(self, counter):
        assert await spend(MYMEMORY_CHARS, 500, 500, day=TODAY) == 500
        assert await spend(MYMEMORY_CHARS, 1, 500, day=TODAY) is None
        assert await spend(MYMEMORY_CHARS, 1, 500, day=TODAY + timedelta(days=1)) == 1
        assert await used(MYMEMORY_CHARS, day=TODAY) == 500

    async def test_budgets_do_not_share_a_counter(self, counter):
        await spend(MYMEMORY_CHARS, 500, 500, day=TODAY)
        assert await spend("groq_requests", 900, 900, day=TODAY) == 900

    async def test_concurrent_spends_respect_the_cap(self, counter):
        """Ten racers, cap of four: the statement is the lock, so exactly four win."""
        import asyncio

        results = await asyncio.gather(
            *(spend(MYMEMORY_CHARS, 1, 4, day=TODAY) for _ in range(10))
        )
        assert sorted(r for r in results if r is not None) == [1, 2, 3, 4]
        assert await used(MYMEMORY_CHARS, day=TODAY) == 4


class TestFailSafe:
    async def test_no_engine_refuses_to_spend(self, monkeypatch):
        monkeypatch.setattr(budget_module, "_get_engine", lambda: None)
        assert await spend(MYMEMORY_CHARS, 1, 100) is None
        assert await used(MYMEMORY_CHARS) is None

    async def test_unreachable_database_refuses_to_spend(self, monkeypatch):
        engine = create_async_engine("sqlite+aiosqlite:////nonexistent/dir/counters.db")
        monkeypatch.setattr(budget_module, "_get_engine", lambda: engine)
        try:
            assert await spend(MYMEMORY_CHARS, 1, 100) is None
            assert await used(MYMEMORY_CHARS) is None
        finally:
            await engine.dispose()

    async def test_missing_table_refuses_to_spend(self, tmp_path, monkeypatch):
        """The migration has not been applied: refuse, do not fall open."""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
        monkeypatch.setattr(budget_module, "_get_engine", lambda: engine)
        try:
            assert await spend(MYMEMORY_CHARS, 1, 100) is None
        finally:
            await engine.dispose()


class TestSyncEntryPoint:
    def test_spend_sync_counts_like_spend(self, counter):
        assert spend_sync(MYMEMORY_CHARS, 120, 200, day=TODAY) == 120
        assert asyncio_run(used(MYMEMORY_CHARS, day=TODAY)) == 120

    def test_spend_sync_returns_none_over_cap(self, counter):
        assert spend_sync(MYMEMORY_CHARS, 200, 200, day=TODAY) == 200
        assert spend_sync(MYMEMORY_CHARS, 1, 200, day=TODAY) is None


def asyncio_run(coro):
    """Run one coroutine from a sync test, which has no running loop of its own."""
    import asyncio

    return asyncio.run(coro)