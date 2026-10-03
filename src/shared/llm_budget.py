"""Daily request + token budget, and in-flight coalescing, for LLMClient's Groq calls.

The counters are rows in budget_counters (src/shared/budget.py), so the caps hold across
every process: two ingest runs a day against one Groq key spend one budget, not two.
When a counter cannot be read, a call is refused rather than sent -- see that module
for why the failure direction is one-way.

Two caps, not one, because the free tier enforces both and they bind at different times.
`daily_limit` counts requests; `token_limit` counts tokens, which is the unit that
actually exhausts the day. Keeping only the request cap is what produced the measured
failure this fixes: a day whose request counter read 28 while the day's 200,000 tokens
were gone, so the cap reported 97% unspent and let every call through to a 429.
"""
from dataclasses import dataclass
import asyncio
import hashlib

from src.shared.budget import (
    COUNTERS_BY_NAME,
    GROQ_REQUEST_TOKENS,
    GROQ_REQUESTS,
    counter_cap,
    spend,
    used,
)

__all__ = ["BudgetExhausted", "BudgetStatus", "RequestBudget", "estimate_tokens"]


def estimate_tokens(messages: list[dict]) -> int:
    """A deliberately pessimistic token estimate for a chat request.

    No tokenizer is a dependency of this project, so this is characters/2 rather than
    characters/4: English averages ~4 characters per token, so dividing by 2
    over-estimates, which is the direction that matters when the estimate gates a spend.
    An estimate that could under-count would let a call through the cap and then be
    discovered by the provider's 429 instead of by us. It also adds the room the
    provider will spend on completion, because the gate has to cover the whole call.

    Mirrors src/verification/phase2.py:estimate_prompt_tokens rather than importing it:
    that function takes a bare prompt string, and importing phase2 from the shared client
    would pull the verification package (and its schema imports) into every LLM caller.
    The two are pinned to the same arithmetic by a test.
    """
    chars = sum(len(str(m.get("content") or "")) for m in messages or [])
    return chars // 2 + 1


@dataclass
class BudgetStatus:
    used_today: int
    limit: int
    remaining: int
    exhausted: bool
    # Token-denominated view of the same day. `exhausted` alone is the trap this batch
    # closes: a request counter far under its cap with the day's tokens gone reported
    # healthy, so both units travel together and callers can see which one ran out.
    tokens_used_today: int = 0
    token_limit: int = 0
    tokens_remaining: int = 0
    tokens_exhausted: bool = False
    # Which unit is the reason for a refusal, for the message the operator reads.
    exhausted_unit: str = "requests"


class BudgetExhausted(Exception):
    def __init__(self, status: BudgetStatus):
        unit = "tokens" if status.exhausted_unit == "tokens" else "requests"
        super().__init__(
            f"Groq daily budget exhausted: {status.used_today}/{status.limit} requests, "
            f"{status.tokens_used_today}/{status.token_limit} tokens "
            f"(refused on {unit})"
        )
        self.status = status


class RequestBudget:
    """Groq's daily request AND token caps, counted in the database.

    There is no local count to fall back on: an int in this process is exactly the thing
    that made the cap per-run. The cost is that status() is async, because reading the
    truth is a query.

    `token_limit` defaults to the configured Groq token cap rather than being required,
    so every existing caller that constructs this with only a request budget keeps
    working and keeps its request behaviour. It resolves from settings instead of being
    read off the client, because LLMClient passes nothing but the request cap and the
    hand-built LLMClient fixture in tests/test_snippet_extractor_live_body.py
    constructs this class directly.
    """

    def __init__(self, daily_limit: int, token_limit: int | None = None):
        self.daily_limit = daily_limit
        if token_limit is None:
            token_limit = counter_cap(COUNTERS_BY_NAME[GROQ_REQUEST_TOKENS])
        self.token_limit = token_limit
        self._inflight: dict[str, "asyncio.Future"] = {}

    async def status(self) -> BudgetStatus:
        spent_requests, spent_tokens = await asyncio.gather(
            used(GROQ_REQUESTS), used(GROQ_REQUEST_TOKENS)
        )
        # Unreadable is not unspent. Report the cap as reached so the caller's next
        # move is the same as for a spent budget. Either unit being unreadable is enough
        # to refuse, because a caller that cannot verify one of its two caps must not
        # spend on the strength of the other.
        if spent_requests is None or spent_tokens is None:
            return BudgetStatus(
                used_today=self.daily_limit, limit=self.daily_limit,
                remaining=0, exhausted=True,
                tokens_used_today=self.token_limit, token_limit=self.token_limit,
                tokens_remaining=0, tokens_exhausted=True,
                exhausted_unit="tokens" if spent_tokens is None else "requests",
            )
        requests_exhausted = spent_requests >= self.daily_limit
        tokens_exhausted = spent_tokens >= self.token_limit
        return BudgetStatus(
            used_today=spent_requests,
            limit=self.daily_limit,
            remaining=max(0, self.daily_limit - spent_requests),
            exhausted=requests_exhausted,
            tokens_used_today=spent_tokens,
            token_limit=self.token_limit,
            tokens_remaining=max(0, self.token_limit - spent_tokens),
            tokens_exhausted=tokens_exhausted,
            exhausted_unit="tokens" if tokens_exhausted else "requests",
        )

    @staticmethod
    def _coalesce_key(messages: list[dict], model: str) -> str:
        raw = repr(messages) + model
        return hashlib.sha256(raw.encode()).hexdigest()

    async def run(self, coro_factory, messages: list[dict], model: str):
        """Run coro_factory() under the budget + coalescing. coro_factory is
        a zero-arg callable returning the awaitable Groq call, so an
        identical in-flight call is awaited a second time instead of
        dispatched twice. Raises BudgetExhausted if either daily cap is spent.

        The whole thing, reservation included, is one future, so the key is registered
        before the counter round trip rather than after it: registering afterwards would
        leave the round trip as a window in which a second identical call sees no key and
        dispatches its own request.
        """
        key = self._coalesce_key(messages, model)
        fut = self._inflight.get(key)
        if fut is None:
            fut = self._inflight[key] = asyncio.ensure_future(self._counted(coro_factory, messages))
        try:
            return await fut
        finally:
            self._inflight.pop(key, None)

    async def _counted(self, coro_factory, messages: list[dict] | None = None):
        """Reserve one request against the day's cap, then make the call.

        Counted before the call, not after: that is the only order in which two
        processes cannot both read "one left" and both send. A call that then fails has
        spent a request it never used, which is the safe direction to be wrong in.

        The token cap is checked here too, as a headroom gate on an estimate, because the
        real token count is only knowable after the call. This is the same two-step shape
        Phase2TokenBudget uses (ensure_headroom then record), and it carries the same
        stated consequence: the reservation is not pre-committed, so a single call may
        overshoot the cap by its own size and the NEXT call is the one refused. The
        alternative -- committing an estimate and refunding the difference -- needs a
        decrement primitive this module deliberately does not have.
        """
        if messages is not None:
            estimate = estimate_tokens(messages)
            remaining = await self.token_headroom()
            if estimate > remaining:
                raise BudgetExhausted(await self.status())
        if await spend(GROQ_REQUESTS, 1, self.daily_limit) is None:
            raise BudgetExhausted(await self.status())
        return await coro_factory()

    async def token_headroom(self) -> int:
        """Tokens left in today's allowance, or 0 when the counter is unreadable."""
        spent = await used(GROQ_REQUEST_TOKENS)
        if spent is None:
            return 0
        return max(0, self.token_limit - spent)

    async def record_tokens(self, total_tokens: int | None) -> int:
        """Charge the provider's reported `usage.total_tokens` against the day's allowance.

        The provider's own number, so reasoning tokens are counted: they are real tokens
        against a real allowance, and the whole reason this counter is denominated in
        tokens rather than requests.

        A missing or zero usage is charged 1 rather than 0. The request happened, and a
        counter that cannot see a call is a counter that stops being a cap. This is also
        why nothing here inspects the completion's content: a reasoning model returns
        empty content intermittently, and a call that spent tokens is spend whether or not
        it produced text.

        The return of spend() is deliberately ignored. By the time this runs the tokens
        are already spent, so refusing the *recording* would not un-spend them, and raising
        here would turn a successful provider call into a caller-visible failure for the
        sake of a counter. The overshoot is visible in the counter and in the daily report,
        which is the honest place for it.
        """
        amount = max(1, int(total_tokens or 0))
        await spend(GROQ_REQUEST_TOKENS, amount, self.token_limit)
        return amount