"""Unit tests for RequestBudget daily request governor + coalescing."""

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from src.shared.llm_budget import RequestBudget, BudgetExhausted


class TestRequestBudget:
    """Tests for RequestBudget."""

    def test_status_initial(self):
        """Initial status shows zero used, full remaining."""
        budget = RequestBudget(daily_limit=100)
        status = budget.status()
        assert status.used_today == 0
        assert status.limit == 100
        assert status.remaining == 100
        assert status.exhausted is False

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_coalescing_different_model(self):
        """Same messages, different model should NOT be coalesced."""
        call_count = 0

        async def tracked_call():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        budget = RequestBudget(daily_limit=100)

        result1 = await budget.run(tracked_call, [{"role": "user", "content": "same"}], "model1")
        result2 = await budget.run(tracked_call, [{"role": "user", "content": "same"}], "model2")

        assert result1 != result2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_utc_day_rollover(self):
        """Assert the counter resets after mocking the clock across a UTC day boundary."""
        budget = RequestBudget(daily_limit=2)

        async def dummy_call():
            return "result"

        # Use 2 budget
        await budget.run(dummy_call, [{"role": "user", "content": "test1"}], "model1")
        await budget.run(dummy_call, [{"role": "user", "content": "test2"}], "model1")

        # Should be exhausted now
        with pytest.raises(BudgetExhausted):
            await budget.run(dummy_call, [{"role": "user", "content": "test3"}], "model1")

        # Mock the clock to next day by directly manipulating _day
        # (since we can't easily mock datetime.now in the budget)
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        budget._day = tomorrow
        budget._count = 0

        # Should work again
        result = await budget.run(dummy_call, [{"role": "user", "content": "test_after_rollover"}], "model1")
        assert result == "result"
        assert budget.status().used_today == 1
        assert budget.status().remaining == 1

    @pytest.mark.asyncio
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

    def test_status_reflects_usage(self):
        """Status should reflect actual usage."""
        budget = RequestBudget(daily_limit=10)

        async def dummy_call():
            return "result"

        asyncio.run(budget.run(dummy_call, [{"role": "user", "content": "test1"}], "model1"))
        asyncio.run(budget.run(dummy_call, [{"role": "user", "content": "test2"}], "model1"))

        status = budget.status()
        assert status.used_today == 2
        assert status.remaining == 8
        assert status.exhausted is False

    @pytest.mark.asyncio
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])