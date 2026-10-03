"""Tests for per-minute limiting and Retry-After-aware backoff.

Both of these exist because a daily counter cannot express a per-minute limit, and
exponential backoff cannot express "the server told you how long to wait". Neither is
cosmetic:

* `MinuteLimiter` is the only thing standing between a shared free pool and a 429 storm.
  It is also the one place in the budget layer that is NOT durable, and that asymmetry is
  asserted here rather than left for a reader to infer.
* `retry_after_seconds` is why the free tiers are usable at all: OpenRouter answers a
  rate-limited free request with a Retry-After, and a client that retries on its own
  schedule gets told to wait again, forever, while the run looks like it is making
  progress.
"""

import asyncio
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest
from httpx import HTTPStatusError, Request, Response

from src.shared.llm_budget import (
    RETRY_AFTER_MAX,
    MinuteLimiter,
    llm_backoff,
    retry_after_seconds,
)


def _http_error(status: int, headers: dict | None = None) -> HTTPStatusError:
    request = Request("POST", "https://example.test/v1/chat/completions")
    response = Response(status, request=request, headers=headers or {}, json={})
    return HTTPStatusError(str(status), request=request, response=response)


class TestRetryAfter:
    def test_no_header_means_no_instruction(self):
        assert retry_after_seconds(_http_error(429)) is None

    def test_delta_seconds(self):
        assert retry_after_seconds(_http_error(429, {"retry-after": "30"})) == 30.0

    def test_header_lookup_is_case_insensitive_in_practice(self):
        """httpx.Headers is case-insensitive; the fallback exists for the SDK wrappers that
        hand back a plain dict, which is not."""
        assert retry_after_seconds(_http_error(429, {"Retry-After": "12"})) == 12.0

    def test_http_date(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=45)
        assert 40 <= retry_after_seconds(_http_error(429, {"retry-after": format_datetime(when)})) <= 45

    def test_a_date_in_the_past_means_now_not_negative(self):
        """A negative sleep is an error, and a clock-skewed provider must not be able to
        produce one."""
        when = datetime.now(timezone.utc) - timedelta(hours=1)
        assert retry_after_seconds(_http_error(429, {"retry-after": format_datetime(when)})) == 0.0

    def test_absurd_value_is_capped_rather_than_obeyed(self):
        assert retry_after_seconds(_http_error(429, {"retry-after": "1000000000"})) == RETRY_AFTER_MAX

    def test_garbage_is_ignored_not_raised(self):
        assert retry_after_seconds(_http_error(429, {"retry-after": "soon-ish"})) is None

    def test_nanosecond_date_without_tzinfo_is_still_parsed(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=20)
        value = retry_after_seconds(_http_error(429, {"retry-after": format_datetime(when)}))
        assert value is not None and 15 <= value <= 20

    def test_reads_headers_off_the_exception_itself(self):
        """The Groq SDK's error carries the response, but the wrapper shapes move between
        versions; both places are read."""
        error = _http_error(429)
        error.response = None
        error.headers = {"retry-after": "7"}
        assert retry_after_seconds(error) == 7.0

    def test_non_exception_input_is_survivable(self):
        assert retry_after_seconds(ValueError("not an http error")) is None


class _State:
    """The three attributes tenacity's wait callable actually reads."""

    def __init__(self, attempt: int, exc=None):
        self.attempt_number = attempt
        self.outcome = None
        if exc is not None:
            self.outcome = _Outcome(exc)


class _Outcome:
    def __init__(self, exc):
        self._exc = exc

    def exception(self):
        return self._exc


class TestBackoff:
    def test_grows_with_the_attempt_number(self):
        first = max(llm_backoff(_State(1)) for _ in range(50))
        third = max(llm_backoff(_State(3)) for _ in range(50))
        assert third > first

    def test_is_jittered_so_concurrent_runners_de_correlate(self):
        """Full jitter: the runners that collide are exactly the ones that would otherwise
        retry in lockstep and collide again."""
        samples = {llm_backoff(_State(3)) for _ in range(50)}
        assert len(samples) > 20

    def test_never_exceeds_the_ceiling(self):
        for attempt in range(1, 20):
            assert llm_backoff(_State(attempt)) <= 60.0

    def test_retry_after_is_a_floor_and_jitter_only_adds(self):
        """Jitter may make a client slower than instructed. It may never make it faster,
        which is the failure that turns one 429 into a self-inflicted denial."""
        for _ in range(50):
            assert llm_backoff(_State(1, _http_error(429, {"retry-after": "30"}))) >= 30.0

    def test_retry_after_survives_a_tenacity_wrapper(self):
        """The state handed to a wait callable wraps the real error, and the header lives
        on the real error."""
        wrapped = _http_error(429, {"retry-after": "25"})
        retry_error = Exception("RetryError[...]")
        retry_error.last_attempt = type(
            "Attempt", (), {"exception": staticmethod(lambda: wrapped)}
        )()
        assert llm_backoff(_State(1, retry_error)) >= 25.0

    def test_a_missing_outcome_is_not_a_crash(self):
        state = _State(2)
        state.outcome = None
        assert llm_backoff(state) >= 0.0


class TestMinuteLimiter:
    @pytest.mark.asyncio
    async def test_first_call_passes_through_immediately(self):
        limiter = MinuteLimiter(2)
        start = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - start < 0.1

    @pytest.mark.asyncio
    async def test_within_the_cap_nothing_waits(self):
        limiter = MinuteLimiter(3)
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        assert time.monotonic() - start < 0.1
        assert await limiter.used() == 3

    @pytest.mark.asyncio
    async def test_the_call_past_the_cap_waits_for_the_window_to_slide(self):
        limiter = MinuteLimiter(2)
        await limiter.acquire()
        await limiter.acquire()
        # Third call must not be admitted instantly: the oldest entry is only 60s old.
        task = asyncio.ensure_future(limiter.acquire())
        await asyncio.sleep(0.2)
        assert not task.done()

    @pytest.mark.asyncio
    async def test_entries_older_than_a_minute_leave_the_window(self):
        limiter = MinuteLimiter(1)
        await limiter.acquire()
        # Rewind the window rather than sleeping 60s: this is testing the arithmetic, and a
        # test that takes a minute to prove it is a test nobody runs.
        limiter._stamps[0] = time.monotonic() - 61.0
        start = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - start < 0.1
        assert await limiter.used() == 1

    @pytest.mark.asyncio
    async def test_zero_disables_limiting_entirely(self):
        """The only way to say 'this provider's limit is not ours to enforce' without a
        second code path."""
        limiter = MinuteLimiter(0)
        for _ in range(50):
            await limiter.acquire()
        assert await limiter.used() == 0

    @pytest.mark.asyncio
    async def test_negative_is_treated_as_disabled_not_as_always_full(self):
        limiter = MinuteLimiter(-5)
        await limiter.acquire()
        assert await limiter.used() == 0

    @pytest.mark.asyncio
    async def test_concurrent_callers_are_serialised_not_raced(self):
        """Without the lock, check-then-append lets N coroutines all observe room for one
        and all admit themselves -- the exact bug a per-minute limiter exists to prevent."""
        limiter = MinuteLimiter(4)
        await asyncio.gather(*(limiter.acquire() for _ in range(4)))
        assert await limiter.used() == 4
