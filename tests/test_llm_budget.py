"""Unit tests for RequestBudget: the DB-backed daily request governor + coalescing.

The counter comes from the autouse budget_counter fixture in conftest.py, so these are
real spends against a real (SQLite) budget_counters table. That is the whole point: the
behaviour that used to be an int in this process is now a row, and "the cap is per day
across processes" has to be testable without a process.
"""

import asyncio
from datetime import timedelta

import pytest

from src.shared.budget import GROQ_REQUESTS, today, used
from src.shared.llm_budget import RequestBudget, BudgetExhausted

# The counter's day is defined once, in src/shared/budget.py, as the UTC day. A test that
# reads back with date.today() -- the *local* day -- asserts against a different row
# whenever the runner's timezone is not UTC, so these two tests failed for the four hours
# a day between local midnight and UTC midnight and passed for the other twenty. Read the
# same day the product spends against instead of re-deriving it here.
TODAY = today()


class TestRequestBudget:
    """Tests for RequestBudget."""

    async def test_status_initial(self):
        """Initial status shows zero used, full remaining."""
        budget = RequestBudget(daily_limit=100)
        status = await budget.status()
        assert status.used_today == 0
        assert status.limit == 100
        assert status.remaining == 100
        assert status.exhausted is False

    async def test_budget_exhausted_raises(self):
        """Drive RequestBudget past daily_limit and assert BudgetExhausted fires."""
        budget = RequestBudget(daily_limit=2)

        async def dummy_call():
            return "result"

        # First two calls succeed
        await budget.run(dummy_call, [{"role": "user", "content": "test1"}], "model1")
        await budget.run(dummy_call, [{"role": "user", "content": "test2"}], "model1")

        # Third call should raise BudgetExhausted
        with pytest.raises(BudgetExhausted) as exc_info:
            await budget.run(dummy_call, [{"role": "user", "content": "test3"}], "model1")

        assert exc_info.value.status.used_today == 2
        assert exc_info.value.status.limit == 2
        assert exc_info.value.status.exhausted is True

    async def test_a_second_budget_shares_the_first_ones_spend(self):
        """The defect this replaced: two processes each got a full cap."""
        first, second = RequestBudget(daily_limit=2), RequestBudget(daily_limit=2)

        async def dummy_call():
            return "result"

        await first.run(dummy_call, [{"role": "user", "content": "a"}], "m")
        await first.run(dummy_call, [{"role": "user", "content": "b"}], "m")

        assert (await second.status()).exhausted is True
        with pytest.raises(BudgetExhausted):
            await second.run(dummy_call, [{"role": "user", "content": "c"}], "m")

    async def test_coalescing_identical_calls(self):
        """Fire two identical concurrent .run() calls and assert coro_factory invoked once."""
        call_count = 0

        async def tracked_call():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # Simulate async work
            return f"result_{call_count}"

        budget = RequestBudget(daily_limit=100)

        # Fire two identical calls concurrently
        task1 = asyncio.create_task(
            budget.run(tracked_call, [{"role": "user", "content": "same"}], "model1")
        )
        task2 = asyncio.create_task(
            budget.run(tracked_call, [{"role": "user", "content": "same"}], "model1")
        )

        result1 = await task1
        result2 = await task2

        # Both should get the same result
        assert result1 == result2
        # Underlying factory should only be called once
        assert call_count == 1
        # ...and so should the budget: one request, not two.
        assert await used(GROQ_REQUESTS, day=TODAY) == 1

    async def test_coalescing_different_messages(self):
        """Different messages should NOT be coalesced."""
        call_count = 0

        async def tracked_call():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        budget = RequestBudget(daily_limit=100)

        # Fire two different calls
        result1 = await budget.run(tracked_call, [{"role": "user", "content": "msg1"}], "model1")
        result2 = await budget.run(tracked_call, [{"role": "user", "content": "msg2"}], "model1")

        assert result1 != result2
        assert call_count == 2

    async def test_coalescing_different_model(self):
        """Same messages, different model should NOT be coalesced."""
        call_count = 0

        async def tracked_call():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        budget = RequestBudget(daily_limit=100)

        # Fire two different calls
        result1 = await budget.run(tracked_call, [{"role": "user", "content": "same"}], "model1")
        result2 = await budget.run(tracked_call, [{"role": "user", "content": "same"}], "model2")

        assert result1 != result2
        assert call_count == 2

    async def test_utc_day_rollover(self):
        """Yesterday's spend does not count against today's cap."""
        budget = RequestBudget(daily_limit=1)

        async def dummy_call():
            return "result"

        assert await budget.run(dummy_call, [{"role": "user", "content": "today"}], "m") == "result"
        with pytest.raises(BudgetExhausted):
            await budget.run(dummy_call, [{"role": "user", "content": "also today"}], "m")

        # Same cap, yesterday's row: the budget is per (name, day), so it starts empty.
        assert await used(GROQ_REQUESTS, day=TODAY - timedelta(days=1)) == 0

    async def test_concurrent_different_calls_not_coalesced(self):
        """Concurrent different calls should each execute."""
        call_count = 0

        async def tracked_call():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return f"result_{call_count}"

        budget = RequestBudget(daily_limit=100)

        # Fire different calls concurrently
        task1 = asyncio.create_task(
            budget.run(tracked_call, [{"role": "user", "content": "msg1"}], "model1")
        )
        task2 = asyncio.create_task(
            budget.run(tracked_call, [{"role": "user", "content": "msg2"}], "model1")
        )

        await task1
        await task2

        assert call_count == 2

    async def test_status_reflects_usage(self):
        """Status should reflect actual usage."""
        budget = RequestBudget(daily_limit=10)

        async def dummy_call():
            return "result"

        await budget.run(dummy_call, [{"role": "user", "content": "test1"}], "model1")
        await budget.run(dummy_call, [{"role": "user", "content": "test2"}], "model1")

        status = await budget.status()
        assert status.used_today == 2
        assert status.remaining == 8
        assert status.exhausted is False

    async def test_budget_exception_contains_status(self):
        """BudgetExhausted exception should contain the status object."""
        budget = RequestBudget(daily_limit=1)

        async def dummy_call():
            return "result"

        await budget.run(dummy_call, [{"role": "user", "content": "test1"}], "model1")

        with pytest.raises(BudgetExhausted) as exc_info:
            await budget.run(dummy_call, [{"role": "user", "content": "test2"}], "model1")

        assert exc_info.value.status is not None
        assert exc_info.value.status.used_today == 1
        assert exc_info.value.status.limit == 1
        assert exc_info.value.status.exhausted is True

    async def test_a_failed_call_still_costs_a_request(self):
        """Reserved before the call, so a provider failure cannot be spent for free."""
        budget = RequestBudget(daily_limit=10)

        async def failing_call():
            raise RuntimeError("provider exploded")

        with pytest.raises(RuntimeError):
            await budget.run(failing_call, [{"role": "user", "content": "boom"}], "m")

        assert await used(GROQ_REQUESTS, day=TODAY) == 1
        # And the in-flight key is released, so the retry is not coalesced into the failure.
        assert budget._inflight == {}

    async def test_unreadable_counter_refuses_rather_than_allows(self, monkeypatch):
        """Fail safe: no counter, no request. Not one free call per run."""
        from src.shared import budget as budget_module

        monkeypatch.setattr(budget_module, "_get_engine", lambda: None)
        budget = RequestBudget(daily_limit=10)

        async def dummy_call():
            return "result"

        with pytest.raises(BudgetExhausted):
            await budget.run(dummy_call, [{"role": "user", "content": "test"}], "model1")
        status = await budget.status()
        assert status.used_today == -1
        assert status.exhausted is False


def test_request_budget_rejects_token_limit():
    """RequestBudget(token_limit=...) must raise TypeError, not silently ignore.
    
    The vm-main API accepted token_limit but did nothing with it. HEAD uses
    a separate TokenBudget. This guard makes the migration loud.
    """
    from src.shared.llm_budget import RequestBudget
    with pytest.raises(TypeError, match="no longer accepts token_limit"):
        RequestBudget(daily_limit=900, token_limit=60000)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
