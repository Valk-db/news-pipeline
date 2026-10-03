"""LLM client with Groq primary, Cerebras fallback, and token accounting."""

import logging
import re
from typing import Optional, List, Dict, Any, Tuple
from src.shared.config import get_settings
from src.shared.llm_budget import RequestBudget, BudgetExhausted
from tenacity import retry, stop_after_attempt, wait_exponential
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
    """Unified LLM client with provider fallback."""

    def __init__(self):
        self.settings = get_settings()
        self.groq_client: Optional[AsyncGroq] = None
        self.cerebras_client: Optional[AsyncCerebras] = None
        self._budget = RequestBudget(self.settings.groq_daily_request_budget)
        # Track which providers have already emitted auth failure warnings this run
        self._groq_auth_warned = False
        self._cerebras_auth_warned = False
        self._init_clients()

    def _init_clients(self):
        if self.settings.groq_api_key and AsyncGroq:
            self.groq_client = AsyncGroq(api_key=self.settings.groq_api_key)
        if self.settings.cerebras_api_key and AsyncCerebras:
            self.cerebras_client = AsyncCerebras(api_key=self.settings.cerebras_api_key)

    @staticmethod
    def _status_code(exception: BaseException) -> int:
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
        return code if isinstance(code, int) else 0

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

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
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
            message = response.choices[0].message
            return {
                "choices": [{"message": {"content": message.content}}],
                # The provider's own accounting of what the call cost. Callers
                # that cap a shared daily token allowance cannot measure it any
                # other way; everything that ignores this key is unaffected.
                "usage": {
                    "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                    "total_tokens": getattr(response.usage, "total_tokens", 0),
                },
                # finish_reason is the only thing that distinguishes "the model
                # answered" from "the model ran out of room", and the difference is
                # invisible once the content is "".
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
            }

        # Route through budget + coalescing
        return await self._budget.run(_do_groq_call, messages, model)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
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
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        if response_format:
            kwargs["response_format"] = response_format
        response = await self.cerebras_client.chat.completions.create(**kwargs)
        return {
            "choices": [{"message": {"content": response.choices[0].message.content}}]
        }

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

        # Try Groq first
        if self.groq_client:
            try:
                result = await self._chat_completion_groq(
                    self.settings.groq_model,
                    messages,
                    max_tokens=300,
                    temperature=0.2,
                )
                caption = result["choices"][0]["message"]["content"].strip()
                is_valid, error = validate_caption(caption, platform, source_texts)
                if is_valid:
                    return caption
                logger.warning("Groq caption validation failed: %s", error)
            except BudgetExhausted as e:
                logger.warning("Groq budget exhausted: %s", e)
            except Exception as e:
                # If it's a transient error (including RetryError wrapping one), re-raise
                from tenacity import RetryError
                if isinstance(e, RetryError):
                    if e.last_attempt:
                        exc = e.last_attempt.exception()
                        if exc and self._is_transient_error(exc):
                            raise LLMError("No LLM provider available")
                elif self._is_transient_error(e):
                    raise LLMError("No LLM provider available")
                # P0-5: Handle auth failures uniformly - fall through to Cerebras
                if self._is_auth_error(e) and not self._groq_auth_warned:
                    import os
                    if os.getenv("GITHUB_ACTIONS"):
                        print(f"::warning title=LLM provider auth failed::Groq authentication failed (401/403): {e}")
                    self._groq_auth_warned = True
                logger.warning("Groq failed: %s", e)

        # Fallback to Cerebras
        if self.cerebras_client:
            try:
                result = await self._chat_completion_cerebras(
                    self.settings.cerebras_model,
                    messages,
                    max_tokens=300,
                    temperature=0.2,
                )
                caption = result["choices"][0]["message"]["content"].strip()
                is_valid, error = validate_caption(caption, platform, source_texts)
                if is_valid:
                    return caption
                logger.warning("Cerebras caption validation failed: %s", error)
            except Exception as e:
                if self._is_auth_error(e) and not self._cerebras_auth_warned:
                    import os
                    if os.getenv("GITHUB_ACTIONS"):
                        print(f"::warning title=LLM provider auth failed::Cerebras authentication failed (401/403): {e}")
                    self._cerebras_auth_warned = True
                logger.warning("Cerebras failed: %s", e)

        # Budget exhausted and no Cerebras fallback - record skip
        if not self.cerebras_client:
            from src.utils.ingest_stats import STATS
            STATS.record("groq", "budget_skipped")
        return None

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

        if self.groq_client:
            try:
                result = await self._chat_completion_groq(
                    self.settings.groq_model,
                    messages,
                    max_tokens=100,
                    temperature=0.1,
                    response_format=response_format,
                )
                content = result["choices"][0]["message"]["content"]
                data = self._parse_json_response(content)
                return float(data.get("score", 0))
            except BudgetExhausted as e:
                logger.warning("Groq budget exhausted: %s", e)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("classify_relevance Groq parse failed: %s", e)
            except Exception as e:
                # If it's a transient error (including RetryError wrapping one), re-raise
                from tenacity import RetryError
                if isinstance(e, RetryError):
                    if e.last_attempt and self._is_transient_error(e.last_attempt.exception()):
                        raise LLMError("No LLM provider available")
                elif self._is_transient_error(e):
                    raise LLMError("No LLM provider available")
                # P0-5: Handle auth failures uniformly - fall through to Cerebras
                if self._is_auth_error(e) and not self._groq_auth_warned:
                    import os
                    if os.getenv("GITHUB_ACTIONS"):
                        print(f"::warning title=LLM provider auth failed::Groq authentication failed (401/403): {e}")
                    self._groq_auth_warned = True
                logger.warning("classify_relevance Groq failed: %s", e)

        if self.cerebras_client:
            try:
                result = await self._chat_completion_cerebras(
                    self.settings.cerebras_model,
                    messages,
                    max_tokens=100,
                    temperature=0.1,
                    response_format=response_format,
                )
                content = result["choices"][0]["message"]["content"]
                data = self._parse_json_response(content)
                return float(data.get("score", 0))
            except Exception as e:
                if self._is_auth_error(e) and not self._cerebras_auth_warned:
                    import os
                    if os.getenv("GITHUB_ACTIONS"):
                        print(f"::warning title=LLM provider auth failed::Cerebras authentication failed (401/403): {e}")
                    self._cerebras_auth_warned = True
                logger.warning("classify_relevance Cerebras failed: %s", e)

        # Budget exhausted and no Cerebras fallback - record skip
        if not self.cerebras_client:
            from src.utils.ingest_stats import STATS
            STATS.record("groq", "budget_skipped")
        return 0.5  # Default neutral

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
        # Try Groq first
        if self.groq_client:
            try:
                return await self._chat_completion_groq(
                    self.settings.groq_model,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                )
            except BudgetExhausted as e:
                logger.warning(f"Groq budget exhausted: {e}")
            except Exception as e:
                # If it's a transient error, re-raise (it will have been retried by decorator)
                if self._is_transient_error(e):
                    raise
                # P0-5: Handle auth failures uniformly - fall through to Cerebras
                if self._is_auth_error(e) and not self._groq_auth_warned:
                    import os
                    if os.getenv("GITHUB_ACTIONS"):
                        print(f"::warning title=LLM provider auth failed::Groq authentication failed (401/403): {e}")
                    self._groq_auth_warned = True
                logger.warning(f"Groq chat completion failed: {e}")

        # Fallback to Cerebras
        if self.cerebras_client:
            try:
                return await self._chat_completion_cerebras(
                    self.settings.cerebras_model,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                # If it's a transient error, re-raise
                if self._is_transient_error(e):
                    raise
                if self._is_auth_error(e) and not self._cerebras_auth_warned:
                    import os
                    if os.getenv("GITHUB_ACTIONS"):
                        print(f"::warning title=LLM provider auth failed::Cerebras authentication failed (401/403): {e}")
                    self._cerebras_auth_warned = True
                logger.warning(f"Cerebras chat completion failed: {e}")

        raise LLMError("No LLM provider available")


# Singleton instance
_llm_client = None


async def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client