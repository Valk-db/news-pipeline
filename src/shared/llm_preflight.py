"""LLM preflight check - one minimal call per provider at workflow start.

Each provider is probed through its OWN client method. Probing through
``LLMClient.chat_completion`` is wrong: that method tries Groq first and silently falls
back to Cerebras, so a dead Groq key was reported healthy whenever Cerebras answered
(and the "cerebras" probe just re-tested Groq).
"""

import re
import sys
from typing import Any, Awaitable, Callable

from src.shared.config import get_settings
from src.shared.llm import get_llm_client

_PING = [{"role": "user", "content": "ping"}]


def _unwrap(exc: BaseException) -> BaseException:
    """tenacity wraps the real error in RetryError; dig the original back out."""
    last_attempt = getattr(exc, "last_attempt", None)
    if last_attempt is not None:
        try:
            inner = last_attempt.exception()
        except Exception:
            inner = None
        if inner is not None:
            return inner
    return exc


def _status_from_exception(exc: BaseException) -> int:
    """Best-effort HTTP status from an SDK/httpx error; 500 when nothing usable is found."""
    exc = _unwrap(exc)
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response_status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(response_status, int):
        return response_status
    match = re.search(r"\b([1-5]\d{2})\b", str(exc))
    return int(match.group(1)) if match else 500


async def _probe(call: Callable[[], Awaitable[Any]]) -> dict:
    try:
        await call()
        return {"status": 200, "ok": True}
    except Exception as e:  # noqa: BLE001 - any failure means the provider is not usable
        inner = _unwrap(e)
        return {
            "status": _status_from_exception(e),
            "ok": False,
            "error": f"{type(inner).__name__}: {inner}"[:100],
        }


async def run_llm_preflight() -> dict:
    """Run a minimal LLM call for each provider to verify authentication.

    Returns:
        dict with provider status: {"groq": {"status": 200, "ok": True}, "cerebras": {"status": 401, "ok": False}}
    """
    settings = get_settings()
    client = await get_llm_client()

    results: dict[str, dict] = {}

    if settings.groq_api_key:
        results["groq"] = await _probe(
            lambda: client._chat_completion_groq(
                settings.groq_model, _PING, max_tokens=1, temperature=0
            )
        )
    else:
        results["groq"] = {"status": None, "ok": False, "configured": False, "error": "No API key configured"}

    if settings.cerebras_api_key:
        results["cerebras"] = await _probe(
            lambda: client._chat_completion_cerebras(
                settings.cerebras_model, _PING, max_tokens=1, temperature=0
            )
        )
    else:
        results["cerebras"] = {"status": None, "ok": False, "configured": False, "error": "No API key configured"}

    return results


async def run_llm_preflight_or_fail() -> None:
    """Run preflight and exit 1 if a configured provider is rejected, or if none works.

    A provider with no API key is "not configured", not "failed": Cerebras is an optional
    backup, so leaving its key unset must not take down the whole run.
    """
    results = await run_llm_preflight()

    failed = [
        f"{provider}: status={result.get('status')}, error={result.get('error')}"
        for provider, result in results.items()
        if not result["ok"] and result.get("configured", True)
    ]
    if failed:
        print(f"LLM preflight failed: {'; '.join(failed)}")
        sys.exit(1)

    if not any(result["ok"] for result in results.values()):
        print("LLM preflight failed: no LLM provider configured (set GROQ_API_KEY and/or CEREBRAS_API_KEY)")
        sys.exit(1)

    working = ", ".join(p for p, r in results.items() if r["ok"])
    print(f"LLM preflight OK: authenticated providers: {working}")