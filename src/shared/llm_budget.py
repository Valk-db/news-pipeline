"""Daily request budget + in-flight coalescing for LLMClient's Groq calls.

In-memory, per-process daily counter -- resets at UTC midnight. This is
correct for the current twice-daily GitHub Actions run (one process per
run, no concurrent processes sharing a Groq key). It is NOT a distributed
limiter: if this pipeline ever runs as more than one concurrent process
against the same Groq key, replace this with a DB-backed counter.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import hashlib


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
    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._count = 0
        self._day = datetime.now(timezone.utc).date()
        self._inflight: dict[str, "asyncio.Future"] = {}

    def _roll_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._count = 0

    def status(self) -> BudgetStatus:
        self._roll_if_new_day()
        remaining = max(0, self.daily_limit - self._count)
        return BudgetStatus(
            used_today=self._count,
            limit=self.daily_limit,
            remaining=remaining,
            exhausted=remaining == 0,
        )

    @staticmethod
    def _coalesce_key(messages: list[dict], model: str) -> str:
        raw = repr(messages) + model
        return hashlib.sha256(raw.encode()).hexdigest()

    async def run(self, coro_factory, messages: list[dict], model: str):
        """Run coro_factory() under the budget + coalescing. coro_factory is
        a zero-arg callable returning the awaitable Groq call, so an
        identical in-flight call is awaited a second time instead of
        dispatched twice. Raises BudgetExhausted if the daily cap is spent."""
        self._roll_if_new_day()
        key = self._coalesce_key(messages, model)
        if key in self._inflight:
            return await self._inflight[key]

        status = self.status()
        if status.exhausted:
            raise BudgetExhausted(status)

        fut = asyncio.ensure_future(coro_factory())
        self._inflight[key] = fut
        try:
            result = await fut
            self._count += 1
            return result
        finally:
            self._inflight.pop(key, None)