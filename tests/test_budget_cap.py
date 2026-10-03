"""The cap has to hold on the day's FIRST spend, not just the second one.

`spend()` is one statement doing an upsert, and the cap lives in a WHERE on the update
path. The insert path -- the one that runs when no row exists yet -- had no such check, so
the day's first request went through whatever the cap was. Two consequences, both real:

* any cap could be exceeded by exactly one request;
* a cap of 0 did not refuse, which is not a cap of 0. It is no cap at all until the second
  request of the day, and a disabled rung that still spends is worse than a disabled one.

These are real spends against the autouse SQLite `budget_counter` fixture, because the
bug is in the SQL and a mocked database cannot see SQL.
"""

import pytest

from src.shared.budget import GROQ_REQUESTS, today, spend, used


class TestCapOnFirstSpend:
    @pytest.mark.asyncio
    async def test_a_cap_of_zero_refuses_the_first_spend(self):
        assert await spend(GROQ_REQUESTS, 1, 0) is None
        assert await used(GROQ_REQUESTS, day=today()) == 0

    @pytest.mark.asyncio
    async def test_a_cap_of_zero_refuses_every_spend(self):
        for _ in range(5):
            assert await spend(GROQ_REQUESTS, 1, 0) is None

    @pytest.mark.asyncio
    async def test_the_first_spend_is_still_bounded_by_the_cap(self):
        """A cap of 3 must refuse a 4th request, and the very first request is the one the
        insert path used to let through unconditionally."""
        for _ in range(3):
            assert await spend(GROQ_REQUESTS, 1, 3) is not None
        assert await spend(GROQ_REQUESTS, 1, 3) is None
        assert await used(GROQ_REQUESTS, day=today()) == 3

    @pytest.mark.asyncio
    async def test_a_single_request_larger_than_the_cap_is_refused(self):
        assert await spend(GROQ_REQUESTS, 5, 4) is None
        assert await used(GROQ_REQUESTS, day=today()) == 0

    @pytest.mark.asyncio
    async def test_a_spend_exactly_at_the_cap_is_allowed(self):
        """Off-by-one in the other direction is its own bug: refusing a spend that lands
        exactly on the cap silently costs a request."""
        assert await spend(GROQ_REQUESTS, 1, 1) == 1
        assert await spend(GROQ_REQUESTS, 1, 1) is None

    @pytest.mark.asyncio
    async def test_a_refused_first_spend_does_not_create_a_row(self):
        """Creating the row on a refused insert would make the next day's status report
        usage that never happened."""
        await spend(GROQ_REQUESTS, 1, 0)
        status = await used(GROQ_REQUESTS, day=today())
        assert status == 0
