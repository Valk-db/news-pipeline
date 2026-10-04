"""LLM client with a provider roster, per-rung budgets, and token accounting.

The provider order lives in exactly one place, `src/shared/llm_roster.ROSTER`, and
every public method here walks it. It used to be written out three times, once per
method, and the copies had drifted: Cerebras spent against no budget at all, and a 429
on one rung raised "No LLM provider available" rather than falling through to the next
one. Both are now properties of the roster rather than of any single call site.
"""

import logging
import os
import re
from typing import Optional, List, Dict, Any, Tuple, Callable
from src.shared.config import get_settings
from src.shared.llm_budget import (
    BudgetExhausted,
    MinuteLimiter,
    RequestBudget,
    TokenBudget,
    TokenBudgetExhausted,
    extract_total_tokens,
    _usage_int,
    llm_backoff,
    unwrap_retry,
    upper_bound_tokens,
)
from src.shared.llm_roster import (
    ROSTER,
    LLMRung,
    live_rungs,
    model_for,
)
from tenacity import retry, stop_after_attempt
from httpx import TimeoutException, ConnectError
import json

try:
    from groq import AsyncGroq
except ImportError:
    AsyncGroq = None

try:
    from cerebras.cloud.sdk import AsyncCerebras
except ImportError:
    AsyncCerebras = None


class LLMError(Exception):
    pass


# Platform character limits
PLATFORM_LIMITS = {
    "twitter": 280,
    "x": 280,
    "bluesky": 300,
    "threads": 500,
    "instagram": 2200,
    "linkedin": 3000,
    "facebook": 63206,
}


logger = logging.getLogger(__name__)


def _first_non_empty(values: Optional[List[str]]) -> str:
    """Return the first non-empty stripped string from values, else ""."""
    for value in values or []:
        text = (value or "").strip()
        if text:
            return text
    return ""


def build_deterministic_caption(
    story_title: str,
    key_facts: Optional[List[str]] = None,
    source_urls: Optional[List[str]] = None,
    platform: str = "twitter",
) -> str:
    """Build a caption without calling an LLM.

    Used when no provider is configured, so curation still works end to end.
    Pure and deterministic: the same inputs always produce the same output.

    The caption is the first key fact (falling back to the story title, then a
    generic label), trimmed so caption plus the primary source URL fits the
    platform character limit.
    """
    base = _first_non_empty(key_facts) or (story_title or "").strip() or "News Update"
    base = " ".join(base.split())
    url = _first_non_empty(source_urls)
    limit = PLATFORM_LIMITS.get((platform or "twitter").lower(), 280)

    if not url:
        return base[:limit].strip()

    # One character of the budget goes to the space before the link.
    room = limit - len(url) - 1
    if room <= 0:
        return url[:limit]
    return f"{base[:room].rstrip()} {url}"


def _extract_ngrams(text: str, n: int = 7) -> set[str]:
    """Extract word n-grams from text."""
    words = text.lower().split()
    if len(words) < n:
        return set()
    return {" ".join(words[i:i+n]) for i in range(len(words) - n + 1)}


def validate_caption(
    caption: str,
    platform: str,
    source_texts: List[str],
    min_ngram_overlap: int = 6,
    allow_override: bool = False,
) -> Tuple[bool, str]:
    """
    Validate a generated caption.

    Returns (is_valid, error_message).
    """
    if not caption or not caption.strip():
        return False, "Caption is empty"

    caption = caption.strip()

    # Check character limit
    limit = PLATFORM_LIMITS.get(platform.lower(), 280)
    if len(caption) > limit:
        return False, f"Caption exceeds {platform} limit of {limit} characters ({len(caption)})"

    # Check paraphrase constraint - n-gram overlap with source texts
    caption_ngrams = _extract_ngrams(caption, min_ngram_overlap)
    if caption_ngrams:
        for source_text in source_texts:
            if not source_text:
                continue
            source_ngrams = _extract_ngrams(source_text, min_ngram_overlap)
            if not source_ngrams:
                continue
            overlap = caption_ngrams & source_ngrams
            if overlap:
                msg = f"Caption shares {len(overlap)} n-gram(s) with source text (min: {min_ngram_overlap} words): {', '.join(list(overlap)[:3])}"
                logger.warning("Caption rejected: %s", msg)
                if allow_override:
                    logger.warning("Override enabled — allowing caption despite n-gram overlap")
                    return True, msg  # Return True with warning message
                return False, msg

    return True, ""


class LLMClient:
    """Unified LLM client that walks the roster in llm_roster.ROSTER."""

    def __init__(self):
        self.settings = get_settings()
        self.groq_client: Optional[AsyncGroq] = None
        self.cerebras_client: Optional[AsyncCerebras] = None
        # One budget per rung, built eagerly from settings. Cerebras and the free tiers
        # used to have no counter, so their spend was invisible and uncapped; the dict
        # is keyed by rung name so a new rung cannot silently join without one.
        self._budgets: Dict[str, RequestBudget] = {}
        self._minute_limiters: Dict[str, MinuteLimiter] = {}
        # One TOKEN budget per rung, same dict-per-rung discipline as _budgets.
        # Separate from _budgets because a request cap and a token cap have
        # different numbers and capping only one is not a budget: on 2026-10-03
        # dev's groq_requests read 28 while the day's 200,000 tokens were spent.
        self._token_budgets: Dict[str, TokenBudget] = {}
        # Rung that last produced a usable answer, and rungs ruled out for the rest of
        # the process. See _candidates for why that is worth the two fields.
        self._pinned: Optional[str] = None
        # The last rung failure, kept so a caller that must raise can chain it.
        self._last_walk_error: Optional[BaseException] = None
        self._demoted: set[str] = set()
        self._auth_warned: set[str] = set()
        self._openrouter_key: Optional[str] = None
        self._openrouter_http = None
        self._init_clients()

    def _init_clients(self):
        if self.settings.groq_api_key and AsyncGroq:
            self.groq_client = AsyncGroq(api_key=self.settings.groq_api_key)
        if self.settings.cerebras_api_key and AsyncCerebras:
            self.cerebras_client = AsyncCerebras(api_key=self.settings.cerebras_api_key)
        if self.settings.openrouter_api_key and self.settings.openrouter_api_key.strip():
            self._openrouter_key = self.settings.openrouter_api_key.strip()

    # ---------------------------------------------------------------- budget

    @staticmethod
    def _rung(name: str) -> LLMRung:
        """Look a rung up by name. A missing name is a programming error, not a fallback."""
        for rung in ROSTER:
            if rung.name == name:
                return rung
        raise LLMError(f"rung {name!r} is missing from the roster")

    def _cap(self, rung: LLMRung, attr: str, default: int) -> int:
        value = getattr(self.settings, attr, None)
        return int(value) if isinstance(value, (int, float)) else default

    def _budget_for(self, rung: LLMRung) -> RequestBudget:
        """The budget for one rung, created on first use.

        Built lazily rather than in __init__ so a rung injected after construction (the
        fallback tests do exactly this) still gets a real budget instead of a KeyError.
        """
        budget = self._budgets.get(rung.name)
        if budget is None:
            limiter = self._minute_limiters.get(rung.name)
            if limiter is None:
                limiter = self._minute_limiters[rung.name] = MinuteLimiter(
                    self._cap(rung, rung.minute_cap_attr, 0) if rung.minute_cap_attr else 0
                )
            budget = self._budgets[rung.name] = RequestBudget(
                self._cap(rung, rung.daily_cap_attr, 0) if rung.daily_cap_attr else 0,
                rung.budget_name,
                limiter,
            )
        return budget

    def _token_budget_for(self, rung: LLMRung) -> TokenBudget:
        """The token budget for one rung, created on first use.

        Built from `rung.budget_name` rather than from a second name on the rung,
        so the token row is derived from the request row and the two cannot drift
        apart or collide. `daily_token_cap_attr` defaults to 0, which means
        "refuse everything" -- deliberately the same conservative direction as an
        unreadable counter, so a rung added to ROSTER without a token cap spends no
        tokens rather than unlimited ones.
        """
        budget = self._token_budgets.get(rung.name)
        if budget is None:
            budget = self._token_budgets[rung.name] = TokenBudget(
                rung.budget_name,
                self._cap(rung, rung.daily_token_cap_attr, 0) if rung.daily_token_cap_attr else 0,
            )
        return budget

    async def _record_usage(self, rung: LLMRung, result: Any) -> None:
        """Charge one completion's real token cost to its rung. Never raises.

        Called from exactly one place -- _walk, immediately after _dispatch returns
        -- so the accounting cannot be skipped by a new caller of the transports and
        cannot be applied twice to one completion. The rung that answered is the
        rung charged, which is the whole reason for recording here rather than
        inside each transport: _dispatch's result dict does not carry which rung
        produced it, and a transport that raised has no usage to record at all.

        Every failure mode is contained, because a counter read must not be able to
        take down a request the provider already answered and billed:

        * no usable `usage` -> charged 1 token on an unknown row (llm_budget)
        * the counter row unreachable -> spend() returns None and the call already
          happened; logging it is all there is to do, and the walk carries on
        * an exception anywhere in here -> logged, walk continues, and the request
          is NOT recorded. A lost charge is bad; refusing to return the answer the
          caller already paid for is worse, and Phase 2's own lesson (5) is that
          the expensive non-repeatable step is the LLM call.
        """
        total = extract_total_tokens(result)
        if total is None:
            total = None  # named, because "unknown" and "0" are different facts
        try:
            budget = self._token_budget_for(rung)
            charged = await budget.record(total)
            if total is None:
                logger.warning(
                    "%s returned no usable usage; charged %s token as a floor and"
                    " counted the call as unpriced", rung.name, charged,
                )
            else:
                logger.debug("%s spent %s tokens", rung.name, charged)
        except Exception as exc:  # noqa: BLE001 - accounting must not fail the call
            logger.warning(
                "could not record token usage for %s: %s: %s",
                rung.name, type(exc).__name__, exc,
            )
            # A swallowed recording failure must not be silent: bump a stat so
            # operators can see the gap. The call itself succeeded; only the
            # accounting failed.
            try:
                from src.utils.ingest_stats import STATS
                STATS.record(rung.name, "usage_record_failed")
            except Exception:
                pass  # stat recording must not fail the call either

    async def _check_token_headroom(self, rung: LLMRung, messages: List[Dict[str, str]],
                                    max_tokens: int) -> None:
        """Refuse this rung before the call if the day's tokens cannot cover it.

        Raises TokenBudgetExhausted, which _walk already handles as a budget
        refusal: demote the rung for the run and take the next one. The estimate is
        an UPPER BOUND on this call (prompt at characters/2, plus the caller's own
        max_tokens ceiling), so refusing when it does not fit is correct, and the
        cap can only be crossed by a call whose real cost beat its own ceiling.

        Silence here would be a defect, not a default: without this check the only
        thing standing between a rung and its provider's 200,000-token day is the
        provider noticing, and a 429 on a shared free pool takes the whole roster
        down with it.
        """
        # Note: empty daily_token_cap_attr means "not token-gated" (skips the check).
        # This contradicts the _token_budget_for docstring which says a missing cap
        # means "refuse everything". test_roster_attr_consistency.py forbids empty
        # for every ROSTER rung, so this path cannot fire for a real rung today.
        # If the guard is removed, empty falls through to _token_budget_for which
        # creates a budget with cap 0, and ensure_headroom raises TokenBudgetExhausted.
        if not rung.daily_token_cap_attr:
            return
        await self._token_budget_for(rung).ensure_headroom(
            upper_bound_tokens(messages, max_tokens)
        )

    # ---------------------------------------------------------------- roster

    def _candidates(self) -> List[LLMRung]:
        """The live rungs, demoted ones removed and the pinned one first.

        The demotion is the reason this is not just `live_rungs`. OpenRouter's free pool
        is shared with every other OpenRouter user, so a rung can be locked out for the
        whole run: measured on 2026-10-03, google/gemma-4-26b-a4b-it:free returned 429
        with `limit_source: upstream_provider_shared_pool` on 3/3 attempts while
        nvidia/nemotron-3-super-120b-a12b:free returned 200 on the first. Without this,
        every article in the batch pays three 429s and two backoffs to rediscover that.
        A dead rung costs one probe per run, and the pinning means the rung that did work
        is tried first for everything after it.
        """
        rungs = [r for r in live_rungs(self) if r.name not in self._demoted]
        if self._pinned:
            rungs.sort(key=lambda r: r.name != self._pinned)
        return rungs

    def _demote(self, rung: LLMRung, why: str) -> None:
        self._demoted.add(rung.name)
        if self._pinned == rung.name:
            self._pinned = None
        logger.warning("LLM rung %s demoted for this run: %s", rung.name, why)

    def _warn_auth_once(self, rung: LLMRung, exc: BaseException) -> None:
        """One GitHub Actions annotation per rung per run, and only the first."""
        if rung.name in self._auth_warned:
            return
        self._auth_warned.add(rung.name)
        if os.getenv("GITHUB_ACTIONS"):
            label = rung.label or rung.name
            print(f"::warning title=LLM provider auth failed::{label} authentication failed (401/403): {exc}")

    # ---------------------------------------------------------------- errors

    @staticmethod
    def _unwrap_retry(exc: BaseException) -> BaseException:
        """The real error behind tenacity's RetryError. See llm_budget.unwrap_retry."""
        return unwrap_retry(exc)

    @classmethod
    def _status_code(cls, exception: BaseException) -> int:
        """The HTTP status behind an SDK or httpx error, or 0 if there isn't one.

        Written as a single lookup rather than two isinstance chains because the
        provider SDKs do not raise httpx's HTTPStatusError: `groq.RateLimitError`
        carries `.status_code` and is not an httpx class at all, so every
        status-code predicate in this class silently returned False for it. That
        is why a 429 from Groq reached the caller as a generic failure instead of
        as the rate limit it was.
        """
        code = getattr(exception, "status_code", None)
        if isinstance(code, int):
            return code
        response = getattr(exception, "response", None)
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
        return cls._status_code_from_chain(exception)

    @staticmethod
    def _status_code_from_chain(exception: BaseException, _depth: int = 0) -> int:
        """The status code of whatever a wrapper is wrapping, 0 if there is none.

        Two wrappers stood between a provider's answer and the code that could read
        it, and both had to be unwrapped before a rate limit was visible as one:

        * `tenacity.RetryError`, which the @retry decorators raise once their
          attempts run out. It carries the real error at `.last_attempt.exception()`.
          Measured on dev 2026-10-03: a 429 that exhausted three internal attempts
          reached the caller as `LLMError("No LLM provider available")` with no
          status code at all, so the Phase 2 runner recorded a generic error and
          never retried or classified it.
        * `raise ... from` chains, because LLMError is raised from the provider
          error it is reporting.

        Bounded to six levels: an exception whose cause chain loops or is very
        deep is a bug elsewhere, and an unbounded walk here would turn a
        classification helper into a hang.
        """
        if _depth >= 6:
            return 0
        inner = getattr(exception, "last_attempt", None)
        inner = getattr(inner, "exception", lambda: None)()
        if inner is not None and inner is not exception:
            code = LLMClient._status_code(inner)
            if code:
                return code
        for linked in (exception.__cause__, exception.__context__):
            if linked is not None and linked is not exception:
                code = LLMClient._status_code(linked)
                if code:
                    return code
        return 0

    @classmethod
    def is_rate_limited(cls, exception: BaseException) -> bool:
        """HTTP 429 -- the provider's per-minute or per-day limit was reached.

        Distinct from BudgetExhausted on purpose: this is the PROVIDER refusing us,
        so the local cap did its job and the number in the database is still an
        accurate account of what was spent. Handle it by waiting out the
        retry-after the provider asked for, not by raising the cap.
        """
        return cls._status_code(exception) == 429

    @classmethod
    def is_out_of_credit(cls, exception: BaseException) -> bool:
        """HTTP 402 -- the account cannot pay for this call.

        Tyler's rule is $0 always, so this must never be "retried" or worked around:
        a 402 means a paid tier is configured somewhere, which is a configuration
        defect to report, not a condition to route around. Callers skip the story
        and record it.
        """
        return cls._status_code(exception) == 402

    @staticmethod
    def _retry_after_seconds(exception: BaseException, default: float) -> float:
        """The provider's own retry-after, in seconds, or `default` if absent.

        Read from the exception first (the SDKs expose `.retry_after` / a headers
        mapping) and only then from the message, because Groq's rate-limit body
        spells it out in prose ("Please try again in 7.2375s") and a caller that
        ignored it would either hammer the limit or sleep for a guessed interval.
        """
        # Walk the wrapper chain as well as the exception itself. A 429 that
        # exhausted the client's internal retries arrives as tenacity's RetryError,
        # whose own message is repr noise -- the provider's "try again in 7.2375s"
        # is on the error it wraps, so a search that stopped at the top level would
        # fall back to a guess every time it mattered most.
        current = exception
        for _level in range(6):
            if current is None:
                break
            found = LLMClient._retry_after_from_one(current, default)
            if found != default:
                return found
            current = LLMClient._inner_exception(current)
        return default

    @staticmethod
    def _inner_exception(exception: BaseException):
        """The exception `exception` wraps, or None."""
        inner = getattr(exception, "last_attempt", None)
        inner = getattr(inner, "exception", lambda: None)()
        if inner is not None and inner is not exception:
            return inner
        for linked in (exception.__cause__, exception.__context__):
            if linked is not None and linked is not exception:
                return linked
        return None

    @staticmethod
    def _retry_after_from_one(exception, default: float) -> float:
        """The retry-after this one exception carries, or `default`."""
        for attr in ("retry_after", "retry_after_seconds"):
            value = getattr(exception, attr, None)
            if isinstance(value, (int, float)) and value >= 0:
                return float(value)
        headers = getattr(exception, "headers", None)
        if headers is not None:
            try:
                value = headers.get("retry-after")
            except AttributeError:
                value = None
            if value is not None:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
        match = re.search(r"try again in\s+([0-9.]+)\s*s", str(exception), re.IGNORECASE)
        if match:
            return float(match.group(1))
        return default

    @staticmethod
    def _is_transient_error(exception: BaseException) -> bool:
        """Check if error is transient (rate limit, 5xx, timeout, connection)."""
        if isinstance(exception, (TimeoutException, ConnectError)):
            return True
        status_code = LLMClient._status_code(exception)
        return status_code >= 500 or status_code == 429

    @staticmethod
    def _is_auth_error(exception: BaseException) -> bool:
        """Check if error is an auth failure (401/403)."""
        if LLMClient._status_code(exception) in (401, 403):
            return True
        # Also check for auth error in exception message (Groq SDK may raise different types)
        error_msg = str(exception).lower()
        return "invalid api key" in error_msg or "unauthorized" in error_msg or "forbidden" in error_msg

    # ---------------------------------------------------------------- transports

    @retry(
        wait=llm_backoff,
        stop=stop_after_attempt(3),
        retry=lambda retry_state: (
            LLMClient._is_transient_error(retry_state.outcome.exception())
            if retry_state.outcome and retry_state.outcome.exception()
            else False
        ),
    )
    async def _chat_completion_groq(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
        response_format: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make a chat completion request to Groq, routed through budget + coalescing."""
        if not self.groq_client:
            raise LLMError("Groq client not initialized")

        async def _do_groq_call():
            kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
            if response_format:
                kwargs["response_format"] = response_format
            # reasoning_effort is not a nicety on a reasoning model: without it
            # gpt-oss spends max_tokens on its own reasoning and returns an EMPTY
            # message content with finish_reason="length". Measured on a real
            # claim-extraction prompt on 2026-10-03: max_tokens=1500 with no
            # reasoning_effort returned 0 characters on one call and 1,054 on the
            # next, with identical parameters -- an unusable stage that looks like
            # it ran. max_tokens=4096 with reasoning_effort="low" returned
            # finish_reason="stop" and complete JSON on every call. Groq rejects
            # the literal "none", so "low" is the floor, not "off".
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            response = await self.groq_client.chat.completions.create(**kwargs)
            # Extract usage BEFORE touching choices[0]. A billed response with
            # malformed choices (empty list, missing message) still cost tokens;
            # the usage must survive even if content extraction fails below.
            # _usage_int rejects non-counts, so malformed readings arrive as
            # "unknown" and are recorded as unpriced, never as zero.
            usage = {
                "prompt_tokens": _usage_int(getattr(response.usage, "prompt_tokens", None)),
                "completion_tokens": _usage_int(getattr(response.usage, "completion_tokens", None)),
                "total_tokens": _usage_int(getattr(response.usage, "total_tokens", None)),
            }
            try:
                message = response.choices[0].message
                content = message.content
                finish_reason = getattr(response.choices[0], "finish_reason", None)
            except (IndexError, AttributeError):
                # Malformed choices: content is unavailable but usage above is
                # already captured. Return it so the wrapper can record the charge.
                content = None
                finish_reason = None
            return {
                "choices": [{"message": {"content": content}}],
                # The provider's own accounting of what the call cost. Callers
                # that cap a shared daily token allowance cannot measure it any
                # other way; everything that ignores this key is unaffected.
                #
                # None, not 0, when the attribute is absent. This used to default to
                # 0, which is the one value that cannot be allowed through: a zero
                # asserts the call was free, and a caller that trusts it under-
                # reports the day while believing it has a cap.
                "usage": usage,
                # finish_reason is the only thing that distinguishes "the model
                # answered" from "the model ran out of room", and the difference is
                # invisible once the content is "".
                "finish_reason": finish_reason,
            }

        # Route through budget + coalescing
        return await self._budget_for(self._rung("groq")).run(_do_groq_call, messages, model)

    @retry(
        wait=llm_backoff,
        stop=stop_after_attempt(3),
        retry=lambda retry_state: (
            LLMClient._is_transient_error(retry_state.outcome.exception())
            if retry_state.outcome and retry_state.outcome.exception()
            else False
        ),
    )
    async def _chat_completion_cerebras(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
        response_format: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Make a chat completion request to Cerebras."""
        if not self.cerebras_client:
            raise LLMError("Cerebras client not initialized")

        # Now counted. This rung previously spent against nothing, which made a
        # Cerebras-only day uncountable and its traffic uncapped.
        async def _do_cerebras_call():
            kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
            if response_format:
                kwargs["response_format"] = response_format
            response = await self.cerebras_client.chat.completions.create(**kwargs)
            # Extract usage before touching choices[0], same as Groq: a billed
            # response with malformed choices must not lose its charge.
            usage = {
                "prompt_tokens": _usage_int(getattr(getattr(response, "usage", None), "prompt_tokens", None)),
                "completion_tokens": _usage_int(getattr(getattr(response, "usage", None), "completion_tokens", None)),
                "total_tokens": _usage_int(getattr(getattr(response, "usage", None), "total_tokens", None)),
            }
            try:
                content = response.choices[0].message.content
            except (IndexError, AttributeError):
                content = None
            return {
                "choices": [{"message": {"content": content}}],
                # Same shape as Groq's, and for the same reason: this rung used to
                # return no usage at all, so its spend was counted in requests only
                # and its token cost was invisible in the database -- which is the
                # exact hole this batch exists to close. `usage` may be absent on
                # the SDK object; that arrives as unpriced, not as zero.
                "usage": usage,
            }

        return await self._budget_for(self._rung("cerebras")).run(_do_cerebras_call, messages, model)

    @retry(
        wait=llm_backoff,
        stop=stop_after_attempt(3),
        retry=lambda retry_state: (
            LLMClient._is_transient_error(retry_state.outcome.exception())
            if retry_state.outcome and retry_state.outcome.exception()
            else False
        ),
    )
    async def _chat_completion_openrouter(
        self,
        rung: LLMRung,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
        response_format: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Make a chat completion request to an OpenRouter rung, over plain httpx.

        Raw httpx rather than a new SDK dependency, for one concrete reason: Retry-After.
        The backoff in llm_budget is only as good as the header it can read, and a
        vendored second implementation of the same HTTP client is the wrong place to
        discover that a provider wants something other than exponential backoff.
        """
        if not self._openrouter_key:
            raise LLMError("OpenRouter client not initialized")
        if self._openrouter_http is None:
            import httpx

            self._openrouter_http = httpx.AsyncClient(
                base_url=self.settings.openrouter_base_url,
                timeout=self.settings.openrouter_timeout,
                headers={"Authorization": f"Bearer {self._openrouter_key}"},
            )

        async def _do_openrouter_call():
            payload: Dict[str, Any] = {
                "model": rung.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if response_format:
                payload["response_format"] = response_format
            response = await self._openrouter_http.post("/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
            # Extract usage BEFORE the empty-content check below. A billed 200 with
            # no content still cost tokens; the usage must survive even if we raise
            # LLMError for the empty completion.
            raw_usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            usage = {
                "prompt_tokens": _usage_int(raw_usage.get("prompt_tokens")),
                "completion_tokens": _usage_int(raw_usage.get("completion_tokens")),
                "total_tokens": _usage_int(raw_usage.get("total_tokens")),
            }
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not content.strip():
                # A 200 with no content is not a completion, it is a model that spent
                # the budget on reasoning tokens and said nothing. Treating it as success
                # would hand the caller an empty caption and hide the rung; the free
                # reasoning models do this reliably at small max_tokens.
                # Usage was extracted above; attach it to the exception so the
                # wrapper can record the charge before re-raising.
                exc = LLMError(f"{rung.model} returned an empty completion")
                exc._billed_usage = usage
                raise exc
            # The raw body's own usage, normalised to the same shape the other two
            # transports return, so one reader handles all three. OpenRouter has
            # been observed to send counts as strings, which is why this goes
            # through _usage_int and not an int() cast. Absent usage -> all None ->
            # recorded as unpriced.
            return {
                "choices": [{"message": {"content": content}}],
                "usage": usage,
            }

        return await self._budget_for(rung).run(_do_openrouter_call, messages, rung.model)

    # ---------------------------------------------------------------- the walk

    async def _dispatch(
        self,
        rung: LLMRung,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        response_format: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One completion from one rung, however that rung is reached."""
        if rung.method == "groq":
            return await self._chat_completion_groq(
                model_for(rung, self.settings), messages,
                max_tokens=max_tokens, temperature=temperature, response_format=response_format,
                reasoning_effort=reasoning_effort,
            )
        if rung.method == "cerebras":
            return await self._chat_completion_cerebras(
                model_for(rung, self.settings), messages,
                max_tokens=max_tokens, temperature=temperature, response_format=response_format,
            )
        return await self._chat_completion_openrouter(
            rung, messages,
            max_tokens=max_tokens, temperature=temperature, response_format=response_format,
        )

    async def _dispatch_and_record(
        self,
        rung: LLMRung,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        response_format: Optional[Dict[str, str]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatch to a rung and record its token usage, as one unit.

        The recording happens immediately after dispatch succeeds, before the
        caller extracts content or validates the response. A malformed response
        that fails content extraction still billed tokens; those must be
        recorded. This is the single seam through which every transport call
        records usage, whether via _walk or direct (preflight).

        If the transport raises after the provider billed the call (e.g., empty
        choices, empty content), it attaches the usage as exc._billed_usage.
        The wrapper records it before re-raising, so the charge is not lost.
        """
        try:
            result = await self._dispatch(
                rung, messages,
                max_tokens=max_tokens, temperature=temperature,
                response_format=response_format, reasoning_effort=reasoning_effort,
            )
        except BaseException as exc:
            # Transport failed after billing: record the usage it carried.
            billed = getattr(exc, "_billed_usage", None)
            if billed is not None:
                await self._record_usage(rung, {"usage": billed})
            raise
        # Record before anything can fail. extract_total_tokens never raises;
        # a response with no usable usage is recorded as unknown, not zero.
        await self._record_usage(rung, result)
        return result

    async def _walk(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        response_format: Optional[Dict[str, str]] = None,
        accept: Optional[Callable[[str], Any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Optional[Tuple[str, Any]]:
        """Try the roster in order and return (rung name, accepted value), or None.

        None and LLMError mean two different things, and the difference is the whole
        reason this returns an Optional instead of just raising:

        * None -- nothing is left to try, and nothing that failed is going to come back
          on its own. A rejected key, an unconfigured provider, a caption the validator
          refused. The caller degrades (0.5, no caption) because there is no point
          retrying within the run.
        * LLMError -- a rung hit a rate limit or a 5xx and burned its retries. That is
          the *opposite* situation: the provider is fine and busy, so a caller that
          answers 0.5 for every article in the batch has turned a transient upstream
          blip into a day of fabricated neutrality that no error ever surfaces. This
          propagates so the run fails visibly and the workflow retries it.

        `accept` turns raw content into the value the caller wants and returns None to
        reject it and fall through -- a caption that fails the paraphrase check, a
        relevance score that will not parse. A rejection does NOT demote the rung: bad
        output on one article says nothing about the next one, and demoting on it would
        let a single malformed response walk the whole batch off the primary provider.

        Every other failure does demote, because by the time it reaches here the rung has
        already retried and the roster has somewhere better to send the work.
        """
        from src.utils.ingest_stats import STATS

        rungs = self._candidates()
        self._last_walk_error = None
        if not rungs:
            return None
        transient = False
        # The last transient failure, so the LLMError raised at the bottom of the
        # walk is a report of what actually went wrong rather than a shrug. A bare
        # raise here drops the chain, and the chain is the only place the 429
        # still exists: by the time a rate limit reaches this point it has been
        # through the retry decorator, so the live exception is tenacity's
        # RetryError and the status code lives two links down. Without `from e`,
        # is_rate_limited() and is_out_of_credit() both answer False on a real
        # provider limit, and the runner cannot tell "you are rate limited" from
        # "this is a bug". Measured 2026-10-03; the old hand-written chain had the
        # `from e`, and replacing it with the walk dropped it.
        last_transient: Optional[BaseException] = None

        for rung in rungs:
            try:
                await self._check_token_headroom(rung, messages, max_tokens)
                result = await self._dispatch_and_record(
                    rung, messages,
                    max_tokens=max_tokens, temperature=temperature, response_format=response_format,
                    reasoning_effort=reasoning_effort,
                )
            except BudgetExhausted as e:
                logger.warning("%s budget exhausted: %s", rung.name, e)
                # Two different diagnoses under one key. "No requests left" and "no
                # tokens left" look identical in a stat and mean opposite things:
                # the first says the day's call count ran out, the second says the
                # day's allowance ran out, which is the one that actually binds
                # (28 requests against a spent 200,000 tokens, 2026-10-03). A single
                # key hides the finding this batch exists to fix.
                STATS.record(
                    rung.name,
                    "token_budget_skipped" if isinstance(e, TokenBudgetExhausted)
                    else "budget_skipped",
                )
                self._demote(rung, f"budget exhausted ({e})")
                continue
            except Exception as e:
                inner = self._unwrap_retry(e)
                if self._is_auth_error(inner):
                    self._warn_auth_once(rung, inner)
                    self._demote(rung, "auth rejected (401/403)")
                elif self._is_transient_error(inner):
                    # The case that used to end the request on the spot: a 429 that
                    # survived its retries raised "No LLM provider available" without
                    # looking at the rest of the roster. On a shared free pool that was
                    # every single request.
                    logger.warning("%s exhausted its retries, falling through: %s", rung.name, e)
                    STATS.record(rung.name, "retries_exhausted")
                    self._demote(rung, "retries exhausted")
                    transient = True
                    last_transient = e
                else:
                    logger.warning("%s failed, falling through: %s", rung.name, e)
                    self._demote(rung, f"{type(inner).__name__}: {inner}"[:120])
                # Remembered even when the failure is not transient. A caller with
                # no degradation to fall back on -- chat_completion -- has to raise,
                # and a bare raise reports "nothing worked" without saying what. An
                # out-of-credit 402 arrives on this path: not transient, so it never
                # sets `transient`, so the walk ends in None, and a $0 rule that
                # cannot see the 402 cannot enforce itself.
                last_transient = last_transient or e
                continue

            content = result["choices"][0]["message"]["content"]
            if content is None:
                # Transport returned None content (malformed choices). The charge
                # was already recorded by _dispatch_and_record. Treat as a failed
                # rung and fall through; never return None as a success.
                logger.warning("%s returned no content, falling through", rung.name)
                self._demote(rung, "empty content")
                continue
            # The one place a completion is charged for its tokens. It sits after
            # the retry wrapper, so a call that burned three attempts is charged
            # Once for the answer that came back -- which is the provider's own
            # figure and therefore includes the tokens the failed attempts spent.
            # Recording now happens in _dispatch_and_record, before content
            # extraction, so a malformed response that fails to parse still
            # charges the rung. The comment about `accept` still holds: a
            # completion the caller then rejects was still paid for.
            if accept is None:
                self._pinned = rung.name
                return rung.name, content

            try:
                value = accept(content)
            except Exception as exc:  # noqa: BLE001 - unusable output is a rejection, not a crash
                logger.warning("%s output unusable: %s", rung.name, exc)
                value = None
            if value is not None:
                self._pinned = rung.name
                return rung.name, value
            logger.warning("%s output rejected by the caller, falling through", rung.name)

        self._last_walk_error = last_transient
        if transient:
            raise LLMError("No LLM provider available") from last_transient
        return None

    async def generate_caption(
        self,
        story_title: str,
        key_facts: List[str],
        source_urls: List[str],
        platform: str = "twitter",
    ) -> Optional[str]:
        """
        Generate a caption for a curated post.
        CRITICAL: Paraphrase only. One link to source. Never reproduce
        more than a short fragment of the scraped article body.
        """
        # Platform-specific constraints
        constraints = {
            "twitter": "Max 280 characters including link.",
            "instagram": "Max 2200 characters, hashtags encouraged, link in bio.",
            "linkedin": "Max 3000 characters, professional tone.",
            "threads": "Max 500 characters, conversational.",
            "bluesky": "Max 300 characters.",
        }

        platform_constraint = constraints.get(platform, "Max 280 characters.")

        # Build prompt with explicit copyright constraint
        prompt = f"""Write a {platform} post about this news story.

STORY: {story_title}
KEY FACTS: {'; '.join(key_facts)}
SOURCE URLS: {source_urls[0] if source_urls else 'N/A'}

CONSTRAINTS:
- {platform_constraint}
- PARAPHRASE ONLY — never reproduce more than a short fragment (≤15 words) of the original article text
- Include exactly ONE link to the primary source at the end
- No hallucination — only use the key facts provided
- Neutral, journalistic tone
- No hashtags unless platform-specific (Instagram/Threads)

OUTPUT: Just the post text, nothing else."""

        messages = [
            {"role": "system", "content": "You are a news summarization assistant. You paraphrase — you never copy."},
            {"role": "user", "content": prompt},
        ]

        # Source texts for validation (from key_facts and story_title)
        source_texts = [story_title] + key_facts

        def _accept_caption(content: str) -> Optional[str]:
            caption = (content or "").strip()
            is_valid, error = validate_caption(caption, platform, source_texts)
            if not is_valid:
                logger.warning("Caption validation failed: %s", error)
                return None
            return caption

        try:
            walked = await self._walk(
                messages,
                max_tokens=300,
                temperature=0.2,
                accept=_accept_caption,
            )
        except LLMError:
            # A rate-limited roster is worth failing the run over; see _walk.
            raise
        if walked is None:
            logger.warning("No LLM provider produced a valid caption")
            return None
        return walked[1]

    async def classify_relevance(
        self,
        title: str,
        body: str,
        topics: Optional[List[str]] = None,
    ) -> float:
        """Classify article relevance to target topics (0-1)."""
        if topics is None:
            topics = ["geopolitics", "international relations", "conflict", "diplomacy", "sanctions"]

        prompt = f"""Rate the relevance of this article to the topics: {', '.join(topics)}

TITLE: {title}
BODY: {body[:1000]}...

Return a JSON object: {{"score": 0.0-1.0, "reason": "brief explanation"}}"""

        messages = [
            {"role": "user", "content": prompt},
        ]

        response_format = {"type": "json_object"}

        def _accept_score(content: str) -> Optional[float]:
            data = self._parse_json_response(content)
            return float(data.get("score", 0))

        try:
            walked = await self._walk(
                messages,
                max_tokens=100,
                temperature=0.1,
                response_format=response_format,
                accept=_accept_score,
            )
        except LLMError:
            # A rate-limited roster is worth failing the run over; see _walk. Returning
            # the neutral 0.5 here instead would rate every article in the batch as
            # unremarkable and report success.
            raise
        if walked is None:
            logger.warning("No LLM provider returned a relevance score")
            return 0.5  # Default neutral
        return walked[1]

    def _parse_json_response(self, content: str) -> Dict[str, Any]:
        """Parse JSON response, handling fenced code blocks if present."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            content = content.removeprefix("json").strip()
        return json.loads(content)

    async def close(self):
        """Close HTTP clients."""
        if self.groq_client:
            await self.groq_client.close()
        if self.cerebras_client:
            await self.cerebras_client.close()
        if self._openrouter_http is not None:
            await self._openrouter_http.aclose()
            self._openrouter_http = None

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 500,
        temperature: float = 0.3,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        General chat completion interface compatible with OpenAI API format.

        Returns:
            Dict with "choices": [{"message": {"content": "..."}}], plus "usage"
            and "finish_reason" from the provider when it reports them.

        reasoning_effort is passed to Groq only. See _chat_completion_groq for why
        a reasoning model needs it: without it the call can return an empty
        message while still being counted against every budget.
        """
        walked = await self._walk(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        if walked is None:
            raise LLMError("No LLM provider available") from self._last_walk_error
        return {"choices": [{"message": {"content": walked[1]}}]}

    def roster_report(self) -> str:
        """The order actually in force, for a log line at the top of a run."""
        from src.shared.llm_roster import roster_summary

        return roster_summary(self.settings)


# Singleton instance
_llm_client = None


async def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client