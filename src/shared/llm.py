"""LLM client with a provider roster, per-rung budgets, and token accounting.

The provider order lives in exactly one place, `src/shared/llm_roster.ROSTER`, and
every public method here walks it. It used to be written out three times, once per
method, and the copies had drifted: Cerebras spent against no budget at all, and a 429
on one rung raised "No LLM provider available" rather than falling through to the next
one. Both are now properties of the roster rather than of any single call site.
"""

import logging
import os
from typing import Optional, List, Dict, Any, Tuple, Callable
from src.shared.config import get_settings
from src.shared.llm_budget import (
    BudgetExhausted,
    MinuteLimiter,
    RequestBudget,
    llm_backoff,
    unwrap_retry,
)
from src.shared.llm_roster import (
    ROSTER,
    LLMRung,
    live_rungs,
    model_for,
)
from tenacity import retry, stop_after_attempt
from httpx import HTTPStatusError, TimeoutException, ConnectError
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
        # Rung that last produced a usable answer, and rungs ruled out for the rest of
        # the process. See _candidates for why that is worth the two fields.
        self._pinned: Optional[str] = None
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

    @staticmethod
    def _is_transient_error(exception: BaseException) -> bool:
        """Check if error is transient (rate limit, 5xx, timeout, connection)."""
        if isinstance(exception, (TimeoutException, ConnectError)):
            return True
        if isinstance(exception, HTTPStatusError):
            # HTTPStatusError has response attribute with status_code
            status_code = getattr(exception.response, "status_code", 0)
            return status_code >= 500 or status_code == 429
        return False

    @staticmethod
    def _is_auth_error(exception: BaseException) -> bool:
        """Check if error is an auth failure (401/403)."""
        if isinstance(exception, HTTPStatusError):
            status_code = getattr(exception.response, "status_code", 0)
            return status_code in (401, 403)
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
    ) -> Dict[str, Any]:
        """Make a chat completion request to Groq, routed through budget + coalescing."""
        if not self.groq_client:
            raise LLMError("Groq client not initialized")

        async def _do_groq_call():
            kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
            if response_format:
                kwargs["response_format"] = response_format
            response = await self.groq_client.chat.completions.create(**kwargs)
            return {
                "choices": [{"message": {"content": response.choices[0].message.content}}]
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
            return {
                "choices": [{"message": {"content": response.choices[0].message.content}}]
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
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not content.strip():
                # A 200 with no content is not a completion, it is a model that spent
                # the budget on reasoning tokens and said nothing. Treating it as success
                # would hand the caller an empty caption and hide the rung; the free
                # reasoning models do this reliably at small max_tokens.
                raise LLMError(f"{rung.model} returned an empty completion")
            return {"choices": [{"message": {"content": content}}]}

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
    ) -> Dict[str, Any]:
        """One completion from one rung, however that rung is reached."""
        if rung.method == "groq":
            return await self._chat_completion_groq(
                model_for(rung, self.settings), messages,
                max_tokens=max_tokens, temperature=temperature, response_format=response_format,
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

    async def _walk(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        response_format: Optional[Dict[str, str]] = None,
        accept: Optional[Callable[[str], Any]] = None,
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
        if not rungs:
            return None
        transient = False

        for rung in rungs:
            try:
                result = await self._dispatch(
                    rung, messages,
                    max_tokens=max_tokens, temperature=temperature, response_format=response_format,
                )
            except BudgetExhausted as e:
                logger.warning("%s budget exhausted: %s", rung.name, e)
                STATS.record(rung.name, "budget_skipped")
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
                else:
                    logger.warning("%s failed, falling through: %s", rung.name, e)
                    self._demote(rung, f"{type(inner).__name__}: {inner}"[:120])
                continue

            content = result["choices"][0]["message"]["content"]
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

        if transient:
            raise LLMError("No LLM provider available")
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
    ) -> Dict[str, Any]:
        """
        General chat completion interface compatible with OpenAI API format.

        Returns:
            Dict with "choices": [{"message": {"content": "..."}}]
        """
        walked = await self._walk(messages, max_tokens=max_tokens, temperature=temperature)
        if walked is None:
            raise LLMError("No LLM provider available")
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