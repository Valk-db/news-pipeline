"""Daily request budget + in-flight coalescing for LLMClient's calls.

The counter is a row in budget_counters (src/shared/budget.py), so the cap holds across
every process: two ingest runs a day against one Groq key spend one budget, not two.
When the counter cannot be read, a request is refused rather than sent -- see that module
for why the failure direction is one-way.

Three things live here rather than in llm.py, because each is a policy about *spending*
and none of them is a policy about *which provider*:

* one budget object per rung, so Cerebras and the free tiers are counted at all
  (Cerebras previously spent against nothing);
* a per-minute sliding window, because a daily cap cannot express "30 a minute";
* the backoff used between retries, which honours the server's own Retry-After instead
  of guessing at it. Guessing is how a client that is told to wait 30s ends up retrying
  at 2s and being told to wait 30s again.
"""
from dataclasses import dataclass
from collections import deque
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import asyncio
import hashlib
import random
import time

from src.shared.budget import GROQ_REQUESTS, spend, used


# Bounds for the retry backoff. The multiplier is the same shape tenacity's
# wait_exponential used, so the number of attempts and the rough pace are unchanged;
# what changed is that the wait is derived from Retry-After when the server sent one.
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
# Retry-After is the server's instruction, not a hint, so it is honoured exactly and
# the jitter is added on top. An untrusted or clock-skewed value is capped rather than
# obeyed: a hostile (or merely broken) 1e9 would otherwise park the run for 31 years.
RETRY_AFTER_MAX = 120.0


@dataclass
class BudgetStatus:
    used_today: int
    limit: int
    remaining: int
    exhausted: bool


class BudgetExhausted(Exception):
    def __init__(self, status: BudgetStatus, name: str = GROQ_REQUESTS):
        super().__init__(
            f"{name} daily budget exhausted: {status.used_today}/{status.limit}"
        )
        self.status = status
        self.name = name


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
                 minute_limiter: MinuteLimiter | None = None):
        self.daily_limit = daily_limit
        self.name = name
        self.minute_limiter = minute_limiter
        self._inflight: dict[str, "asyncio.Future"] = {}

    async def status(self) -> BudgetStatus:
        spent = await used(self.name)
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
        a zero-arg callable returning the awaitable provider call, so an
        identical in-flight call is awaited a second time instead of
        dispatched twice. Raises BudgetExhausted if the daily cap is spent.

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
        if await spend(self.name, 1, self.daily_limit) is None:
            raise BudgetExhausted(await self.status(), self.name)
        return await coro_factory()
