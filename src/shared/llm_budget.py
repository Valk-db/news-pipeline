"""Daily request + token budget, and in-flight coalescing, for LLMClient's calls.

The counters are rows in budget_counters (src/shared/budget.py), so the caps hold across
every process: two ingest runs a day against one Groq key spend one budget, not two.
When a counter cannot be read, a call is refused rather than sent -- see that module
for why the failure direction is one-way.

Three things live here rather than in llm.py, because each is a policy about *spending*
and none of them is a policy about *which provider*:

* one budget object per rung, so Cerebras and the free tiers are counted at all
  (Cerebras previously spent against nothing);
* a per-minute sliding window, because a daily cap cannot express "30 a minute";
* the backoff used between retries, which honours the server's own Retry-After instead
  of guessing at it.

Two caps per rung, not one, because the free tier enforces both and they bind at
different times. `daily_limit` counts requests; `token_limit` counts tokens, which is
the unit that actually exhausts the day.
"""
from dataclasses import dataclass
from collections import deque
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import asyncio
import hashlib
import random
import time

from src.shared.budget import (
    COUNTERS_BY_NAME,
    GROQ_REQUEST_TOKENS,
    GROQ_REQUESTS,
    counter_cap,
    record,
    spend,
    token_counter_name,
    unpriced_calls_counter_name,
    used,
)


# Bounds for the retry backoff. The multiplier is the same shape tenacity's
# wait_exponential used, so the number of attempts and the rough pace are unchanged;
# what changed is that the wait is derived from Retry-After when the server sent one.
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
# Retry-After is the server's instruction, not a hint, so it is honoured exactly and
# the jitter is added on top. An untrusted or clock-skewed value is capped rather than
# obeyed: a hostile (or merely broken) 1e9 would otherwise park the run for 31 years.
RETRY_AFTER_MAX = 120.0

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
    def __init__(self, status: BudgetStatus, name: str = GROQ_REQUESTS,
                 message: str | None = None):
        super().__init__(
            message or f"{name} daily budget exhausted: {status.used_today}/{status.limit}"
        )
        self.status = status
        self.name = name


class TokenBudgetExhausted(BudgetExhausted):
    """The day's token allowance is spent, or cannot be read.

    A subclass of BudgetExhausted on purpose: LLMClient._walk already knows how to
    demote a rung and move to the next one when a budget is exhausted, and a new
    exception type that is NOT a BudgetExhausted would be caught by the generic
    handler and reported as "failed, falling through" -- indistinguishable from a
    provider error, so a rung with no tokens left would look broken rather than
    capped, and the run would keep retrying it.
    """

    def __init__(self, message: str, name: str, used_today: int, limit: int):
        super().__init__(
            BudgetStatus(used_today=used_today, limit=limit, remaining=0, exhausted=True),
            name,
            message=message,
        )


def _usage_int(value: object) -> int | None:
    """A non-negative token count from a provider's usage object, or None.

    Strict on purpose. `isinstance(True, int)` is True in Python, so a bool
    `total_tokens` would silently become 1 and a negative number would become a
    negative spend -- and `budget_counters.used` is a plain BIGINT with no CHECK
    constraint, so a negative write is accepted by the database and every later
    read of that counter is wrong. A digit string is accepted because OpenRouter
    has been observed to send counts that way; anything else is "unknown", which
    is the answer this function exists to be able to give.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def extract_total_tokens(result: object) -> int | None:
    """The tokens a completed request cost, or None if the provider did not say.

    Reads a transport's completion dict rather than a provider SDK object, because
    `_dispatch` hands one back whichever rung answered and the three transports
    do not agree on shape (Groq builds the dict from SDK attributes; OpenRouter
    passes the raw JSON body through).

    Three cases, in order:

    * `usage.total_tokens` present -> that, and the components are NOT added to it.
      Adding them is the double count the brief names: a provider that sends all
      three fields is not reporting the same tokens twice, and a request that
      reports both is the one shape that would produce a number twice the bill.
    * `total_tokens` absent but prompt/completion present -> their sum. A provider
      that itemises without totalling is under-reporting, not free.
    * nothing usable -> None. The caller records that as unknown. It is never 0:
      0 asserts the call was free, which is a claim nothing in the response
      supports, and it is the value that turns a cap into decoration.
    """
    if not isinstance(result, dict):
        return None
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    total = _usage_int(usage.get("total_tokens"))
    if total is not None:
        return total
    parts = [_usage_int(usage.get("prompt_tokens")), _usage_int(usage.get("completion_tokens"))]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


# A recording is a statement about the past, not a gate on the future: the gate is
# ensure_headroom(), before the call. Refusing to record a call that already
# happened would leave today's spend understated, and an understated counter is
# worse than an overshot one -- it makes the NEXT gate think there is allowance
# left. So record() is given effectively no ceiling. The overshoot this permits is
# bounded by the inaccuracy of the caller's estimate, not by unbounded growth,
# because the pre-call gate stops the request after the one that overshot.
_NO_GATE = 2 ** 62


class TokenBudget:
    """One rung's daily allowance in TOKENS, counted in the database.

    The same two-step shape as src.verification.phase2.Phase2TokenBudget, made
    general so the two cannot drift:

    1. `ensure_headroom(estimate)` before the call, refusing if the call's upper
       bound would not fit in what is left. This is the gate.
    2. `record(usage)` after it, charging what the provider actually reported, so
       the counter tracks real spend rather than an estimate.

    **The cap decision, stated once so it is not re-litigated per caller: the
    check is BOTH, and the estimate is an upper bound rather than a guess.**

    `max_tokens` is a hard ceiling -- the provider will not bill more completion
    tokens than it was asked for -- and the prompt side is estimated at
    characters/2, which over-estimates English's ~4 characters per token. So the
    estimate is the most the call can cost, and refusing when that does not fit
    is *correct* rather than pessimistic. That is what bounds the overshoot: the
    only way to exceed the cap is for the estimate to be too LOW, which a
    deliberately pessimistic prompt estimate cannot be.

    Why not reserve-and-refund the estimate the way `RequestBudget` reserves a
    request: `src/shared/budget.py` has no refund primitive, so a reservation is
    either the final charge (an over-estimate silently starves the job at half
    its granted budget) or needs a decrement that is new shared-primitive risk.
    Two Phase 2 runs could also each pass the gate on the same last request's
    worth of headroom; that race is real, stated, and is why every token cap in
    config.py sits far below its provider's published limit rather than at it.

    An unreadable counter is treated as fully spent, the same direction
    budget.py commits to everywhere else: a budget that cannot be verified must
    not become an unlimited one.
    """

    def __init__(self, request_counter: str, daily_limit: int):
        self.name = token_counter_name(request_counter)
        self.request_counter = request_counter
        self.daily_limit = daily_limit
        self.unpriced_name = unpriced_calls_counter_name(self.name)

    async def spent_today(self) -> int:
        """Tokens charged to this rung today, or -1 when the counter is unreadable."""
        value = await used(self.name)
        return -1 if value is None else value

    async def remaining(self) -> int:
        spent = await self.spent_today()
        return 0 if spent < 0 else max(0, self.daily_limit - spent)

    async def ensure_headroom(self, estimate: int) -> int:
        """Raise TokenBudgetExhausted unless `estimate` fits in what is left today."""
        remaining = await self.remaining()
        if estimate > remaining:
            spent = await self.spent_today()
            raise TokenBudgetExhausted(
                f"estimated {estimate} tokens exceeds {remaining} remaining of the "
                f"{self.daily_limit}/day {self.name} allowance",
                self.name,
                # -1 is "unreadable", which is not a spend figure; report the cap so
                # the message cannot be read as a measured overspend.
                self.daily_limit if spent < 0 else spent,
                self.daily_limit,
            )
        return estimate

    async def record(self, total_tokens: int | None) -> int:
        """Charge what the call actually cost. Returns the amount charged.

        `None` means the provider did not report usage. That records as 1 token
        plus a call on the unpriced row, never 0: the call happened, the true
        cost is somewhere above nothing, and the only honest floor available is
        one token. The 1 makes today's figure a lower bound, which is safe --
        a cap stops early rather than late -- and the unpriced row is what keeps
        that from being invisible.
        """
        known = _usage_int(total_tokens) if total_tokens is not None else None
        amount = known if known and known > 0 else 1
        if known is None or known == 0:
            await spend(self.unpriced_name, 1, _NO_GATE)
        await spend(self.name, amount, _NO_GATE)
        return amount

    async def record_unknown(self) -> int:
        """A completion arrived with no usable usage at all. See record()."""
        return await self.record(None)


def upper_bound_tokens(messages: object, max_tokens: int) -> int:
    """The most tokens a call with this prompt and this ceiling can cost.

    Deliberately pessimistic, and the pessimism is the feature: this number
    gates a spend, so a low estimate lets a call through the cap and the caller
    learns about it from the provider's 429 instead of from us. `max_tokens` is
    the provider's own ceiling on the completion, so that term needs no fudge at
    all; only the prompt side is estimated, at characters/2 against English's
    ~4 characters per token. Reuses src.verification.phase2's estimator shape and
    its reasoning; if that one moves, this should move with it.
    """
    try:
        text = repr(messages)
    except Exception:  # noqa: BLE001 - an unprintable prompt is still a prompt
        text = ""
    return len(text) // 2 + 1 + max(0, int(max_tokens))


def unwrap_retry(exc: BaseException) -> BaseException:
    """tenacity wraps the real error in RetryError; dig the original back out.

    Lives here rather than in llm.py and llm_preflight.py because two of the three places
    that need it are about the *response* -- the status, the Retry-After header -- and a
    RetryError has neither. Classifying or reading the wrapper instead of what it wraps
    is how "429, retries exhausted" quietly comes out looking like an unknown error.
    """
    last_attempt = getattr(exc, "last_attempt", None)
    if last_attempt is not None:
        try:
            inner = last_attempt.exception()
        except Exception:  # noqa: BLE001 - an attempt with no result is not an unwrap
            inner = None
        if inner is not None:
            return inner
    return exc


def _retry_after_header(exc: BaseException) -> str | None:
    """The Retry-After header on a failed request, or None.

    Looks on the exception itself as well as on its `.response`: the Groq SDK wraps
    httpx, and the shape of what carries the response is exactly the sort of thing
    that changes between SDK versions. A missing header is normal, not an error.
    """
    exc = unwrap_retry(exc)
    for source in (exc, getattr(exc, "response", None)):
        headers = getattr(source, "headers", None)
        if headers is None:
            continue
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001 - exotic header containers are simply "no header"
            continue
        if value:
            return str(value)
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """How many seconds a failed request asked us to wait, or None if it did not ask.

    Handles both forms of Retry-After: delta-seconds, and an HTTP-date (which is
    converted against the current clock and is therefore only as good as both clocks
    agree; a date in the past means "now", not "negative").
    """
    raw = _retry_after_header(exc)
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, min(float(raw), RETRY_AFTER_MAX))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), RETRY_AFTER_MAX))


def llm_backoff(retry_state) -> float:
    """Tenacity `wait` callable: exponential with jitter, and Retry-After wins.

    Full jitter (uniform over 0..delay) when the server gave no instruction, because
    the runners that collide are the ones that would otherwise retry in lockstep. When
    it *did* give one, the floor is the server's number and the jitter is added on top:
    jitter may only ever make a client slower than it was told to be.
    """
    attempt = max(1, int(getattr(retry_state, "attempt_number", 1) or 1))
    delay = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)
    outcome = getattr(retry_state, "outcome", None)
    exception = None
    if outcome is not None:
        try:
            exception = outcome.exception()
        except Exception:  # noqa: BLE001 - a future without a result is not a Retry-After
            exception = None
    asked = retry_after_seconds(exception) if exception is not None else None
    if asked is None:
        return random.uniform(0.0, delay)
    return min(asked + random.uniform(0.0, 1.0), _BACKOFF_MAX)


class MinuteLimiter:
    """Sliding one-minute window, in process.

    Deliberately not a database row. A daily cap has to outlive the process, so it is
    durable; a per-minute cap only has to hold inside one burst, and the cost of making
    it durable is a round trip on the critical path of every single request. The
    consequence is stated rather than hidden: two runners in the same minute can
    together exceed the cap, by at most one window's worth. That is the right trade
    against a database query per LLM call, and it is a trade, not an oversight.

    per_minute <= 0 disables limiting entirely, which is the only way to say "this
    provider's limit is not ours to enforce" without a second code path.
    """

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until this request is inside the window, then record it."""
        if self.per_minute <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    return
                # Wait for the oldest request to age out rather than for a fixed second:
                # the window is a sliding one, so a fixed sleep is either too short
                # (and the next loop iteration sleeps again) or needlessly long.
                wait = 60.0 - (now - self._stamps[0])
            await asyncio.sleep(max(wait, 0.05))

    async def used(self) -> int:
        """Requests inside the current window. For logs and tests, not for enforcement."""
        async with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            return len(self._stamps)


class RequestBudget:
    """One rung's daily request cap, counted in the database.

    There is no local count to fall back on: an int in this process is exactly the thing
    that made the cap per-run. The cost is that status() is async, because reading the
    truth is a query.

    `name` is the budget_counters row. It defaults to groq_requests so that the existing
    positional construction in the tests -- RequestBudget(daily_limit) -- keeps meaning
    what it meant, and so that Groq's cap cannot move by accident when a new rung is
    added.
    """

    def __init__(self, daily_limit: int, name: str = GROQ_REQUESTS,
                 minute_limiter: MinuteLimiter | None = None,
                 token_limit: int | None = None):
        self.daily_limit = daily_limit
        self.name = name
        self.minute_limiter = minute_limiter
        self._inflight: dict[str, "asyncio.Future"] = {}
        # token_limit for backwards compatibility with vm-main's API.
        # HEAD's architecture uses separate TokenBudget, but vm-main's tests
        # construct RequestBudget with token_limit directly.
        self.token_limit = token_limit if token_limit is not None else 0

    async def token_headroom(self) -> int:
        """Tokens left in today's allowance, or 0 when the counter is unreadable."""
        from src.shared.budget import GROQ_REQUEST_TOKENS
        spent = await used(GROQ_REQUEST_TOKENS)
        if spent is None:
            return 0
        return max(0, self.token_limit - spent)

    async def record_tokens(self, total_tokens: int | None) -> int:
        """Charge the provider's reported usage against the day's token allowance."""
        from src.shared.budget import GROQ_REQUEST_TOKENS, record
        if total_tokens is None:
            total_tokens = 1  # Charge 1 for unpriced calls (conservative)
        await record(GROQ_REQUEST_TOKENS, total_tokens)
        return total_tokens

    async def status(self) -> BudgetStatus:
        spent = await used(self.name)
        if spent is None:
            # Unreadable is not unspent. Report the cap as reached so the caller's next
            # move is the same as for a spent budget.
            return BudgetStatus(used_today=self.daily_limit, limit=self.daily_limit,
                                remaining=0, exhausted=True)
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
        a zero-arg callable returning the awaitable provider call, so an
        identical in-flight call is awaited a second time instead of
        dispatched twice. Raises BudgetExhausted if either daily cap is spent.

        The whole thing, reservation included, is one future, so the key is registered
        before the counter round trip rather than after it: registering afterwards would
        leave the round trip as a window in which a second identical call sees no key and
        dispatches its own request.
        """
        if self.minute_limiter is not None:
            # Before the daily reservation, not after: the point of the window is that
            # a request that has to wait for a slot should not have already been counted
            # against the day, because a caller that gives up while waiting would burn a
            # day it never spent.
            await self.minute_limiter.acquire()
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
        if await spend(self.name, 1, self.daily_limit) is None:
            raise BudgetExhausted(await self.status(), self.name)
        return await coro_factory()
