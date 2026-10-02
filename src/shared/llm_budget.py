"""Daily request budget + in-flight coalescing for LLMClient's Groq calls.

The counter is a row in budget_counters (src/shared/budget.py), so the cap holds across
every process: two ingest runs a day against one Groq key spend one budget, not two.
When the counter cannot be read, a request is refused rather than sent -- see that module
for why the failure direction is one-way.
"""
from dataclasses import dataclass
import asyncio
import hashlib

from src.shared.budget import GROQ_REQUESTS, spend, used


@dataclass
class BudgetStatus:
    used_today: int
    limit: int
    remaining: int
    exhausted: bool


class BudgetExhausted(Exception):
    def __init__(self, status: BudgetStatus):
        super().__init__(
            f"Groq daily budget exhausted: {status.used_today}/{status.limit}"
        )
        self.status = status


class RequestBudget:
    """Groq's daily request cap, counted in the database.

    There is no local count to fall back on: an int in this process is exactly the thing
    that made the cap per-run. The cost is that status() is async, because reading the
    truth is a query.
    """

    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._inflight: dict[str, "asyncio.Future"] = {}

    async def status(self) -> BudgetStatus:
        spent = await used(GROQ_REQUESTS)
        if spent is None:
            # Unreadable is not unspent. Report the cap as reached so the caller's next
            # move is the same as for a spent budget.
            return BudgetStatus(used_today=self.daily_limit, limit=self.daily_limit,
                                remaining=0, exhausted=True)
        return BudgetStatus(
            used_today=spent,
            limit=self.daily_limit,
            remaining=max(0, self.daily_limit - spent),
            exhausted=spent >= self.daily_limit,
        )

    @staticmethod
    def _coalesce_key(messages: list[dict], model: str) -> str:
        raw = repr(messages) + model
        return hashlib.sha256(raw.encode()).hexdigest()

    async def run(self, coro_factory, messages: list[dict], model: str):
        """Run coro_factory() under the budget + coalescing. coro_factory is
        a zero-arg callable returning the awaitable Groq call, so an
        identical in-flight call is awaited a second time instead of
        dispatched twice. Raises BudgetExhausted if the daily cap is spent.

        The whole thing, reservation included, is one future, so the key is registered
        before the counter round trip rather than after it: registering afterwards would
        leave the round trip as a window in which a second identical call sees no key and
        dispatches its own request.
        """
        key = self._coalesce_key(messages, model)
        fut = self._inflight.get(key)
        if fut is None:
            fut = self._inflight[key] = asyncio.ensure_future(self._counted(coro_factory))
        try:
            return await fut
        finally:
            self._inflight.pop(key, None)

    async def _counted(self, coro_factory):
        """Reserve one request against the day's cap, then make it.

        Counted before the call, not after: that is the only order in which two
        processes cannot both read "one left" and both send. A call that then fails has
        spent a request it never used, which is the safe direction to be wrong in.
        """
        if await spend(GROQ_REQUESTS, 1, self.daily_limit) is None:
            raise BudgetExhausted(await self.status())
        return await coro_factory()