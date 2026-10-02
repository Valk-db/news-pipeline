"""LLM preflight check - one minimal call per provider at workflow start.

Each provider is probed through its OWN client method. Probing through
``LLMClient.chat_completion`` is wrong: that method tries Groq first and silently falls
back to Cerebras, so a dead Groq key was reported healthy whenever Cerebras answered
(and the "cerebras" probe just re-tested Groq).
"""

import os
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


# How to read each unhealthy status. 402 is its own case on purpose: the key is valid, the
# account is just empty, so "rotate the key" would be the wrong fix.
_UNHEALTHY_REASONS = {
    401: "auth rejected (bad key)",
    402: "out of credit / billing dead — treated as unavailable",
    403: "auth rejected (bad key)",
}


def _reason(result: dict) -> str:
    """Why a provider is unusable, classified so a dead optional fallback reads correctly."""
    if not result.get("configured", True):
        return "not configured (optional fallback)"
    return _UNHEALTHY_REASONS.get(result.get("status")) or result.get("error", "unknown error")


def _detail(result: dict) -> str:
    """One report line for an unhealthy provider: the status code and the classified reason."""
    reason = _reason(result)
    status = result.get("status")
    return f"{status} {reason}" if status is not None else reason


def _warn(provider: str, reason: str) -> None:
    """LOUD, and a GitHub Actions annotation when in CI so it shows up in the run UI."""
    message = f"{provider} unavailable: {reason}"
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::warning title=LLM provider unavailable::{message}")
    else:
        print(f"WARNING: {message}")


async def run_llm_preflight_or_fail() -> None:
    """Fail the run only when NO provider is usable; degrade loudly when some are.

    Mirrors ``LLMClient.chat_completion``, which tries Groq, falls through to Cerebras on
    any failure, and only gives up when both are down. A billing-dead Cerebras fallback
    (402) is exactly the case the run must survive: Groq is healthy, so the ingest
    continues and the dead provider is reported rather than fatal.
    """
    results = await run_llm_preflight()
    healthy = [p.capitalize() for p, r in results.items() if r["ok"]]
    degraded = {p.capitalize(): _detail(r) for p, r in results.items() if not r["ok"]}

    print("LLM preflight status:")
    for provider, result in results.items():
        name = provider.capitalize()
        detail = result["status"] if result["ok"] else degraded[name]
        print(f"  {name:<9} {'OK' if result['ok'] else 'DEGRADED':<9} {detail}")

    if not healthy:
        print("LLM preflight failed: no usable LLM provider")
        sys.exit(1)

    for name, detail in degraded.items():
        _warn(name, detail)

    lost = "; ".join(f"{name} unavailable ({detail})" for name, detail in degraded.items())
    print(f"LLM preflight OK: continuing on {', '.join(healthy)}" + (f"; {lost}" if lost else ""))
