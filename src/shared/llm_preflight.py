"""LLM preflight check - one minimal call per provider at workflow start.

Each provider is probed through its OWN client method. Probing through
``LLMClient.chat_completion`` is wrong: that method walks the roster and silently falls
through, so a dead Groq key was reported healthy whenever Cerebras answered (and the
"cerebras" probe just re-tested Groq). The same trap applies to the free tiers, one rung
per rung: probing them through the walk would let a healthy Groq answer for all of them.

Which providers to probe is read from ``llm_roster.ROSTER`` rather than written out
here, because a probe list that is a second copy of the provider list is a second thing
to forget to update -- and a rung that preflight never probes is a rung whose bad
credential only shows up as a mysterious fallback to something else.
"""

import os
import re
import sys
from typing import Any, Awaitable, Callable

from src.shared.config import get_settings
from src.shared.llm import get_llm_client
from src.shared.llm_budget import unwrap_retry
from src.shared.llm_roster import (
    ROSTER,
    LLMRung,
    check_free_model_roster,
    configured_rungs,
    model_for,
)

_PING = [{"role": "user", "content": "ping"}]
# 1 token is enough to prove a credential, except on the free tiers: the reasoning
# models there spend a tiny max_tokens budget on reasoning and return no content at all,
# which would be reported as a failed provider. 16 is still trivial in tokens and
# distinguishes "the key works" from "the model needed room to answer".
_PING_MAX_TOKENS = {"openrouter": 16}
_PING_MAX_TOKENS_DEFAULT = 1


def _unwrap(exc: BaseException) -> BaseException:
    """tenacity wraps the real error in RetryError; dig the original back out."""
    return unwrap_retry(exc)


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


def _probe_call(client, settings, rung: LLMRung) -> Callable[[], Awaitable[Any]]:
    """The one call that exercises exactly this rung, with no fallback behind it."""
    max_tokens = _PING_MAX_TOKENS.get(rung.method, _PING_MAX_TOKENS_DEFAULT)
    model = model_for(rung, settings)
    if rung.method == "groq":
        return lambda: client._chat_completion_groq(
            model, _PING, max_tokens=max_tokens, temperature=0
        )
    if rung.method == "cerebras":
        return lambda: client._chat_completion_cerebras(
            model, _PING, max_tokens=max_tokens, temperature=0
        )
    return lambda: client._chat_completion_openrouter(
        rung, _PING, max_tokens=max_tokens, temperature=0
    )


async def run_llm_preflight() -> dict:
    """Run a minimal LLM call for each configured provider to verify authentication.

    Returns:
        dict with provider status: {"groq": {"status": 200, "ok": True}, "cerebras": {"status": 401, "ok": False}}
    """
    settings = get_settings()
    client = await get_llm_client()
    configured = {r.name for r in configured_rungs(settings)}

    results: dict[str, dict] = {}
    for rung in ROSTER:
        if rung.name in configured:
            results[rung.name] = await _probe(_probe_call(client, settings, rung))
        else:
            # Rungs with no credential still get a row, so a key that was expected and is
            # missing reads as a missing key rather than as a provider never checked.
            results[rung.name] = {
                "status": None, "ok": False, "configured": False,
                "error": "No API key configured",
            }

    return results


def _unconfigured_rungs(settings) -> list[LLMRung]:
    return [r for r in ROSTER if r.name not in {x.name for x in configured_rungs(settings)}]


# How to read each unhealthy status. 402 is its own case on purpose: the key is valid, the
# account is just empty, so "rotate the key" would be the wrong fix.
_UNHEALTHY_REASONS = {
    401: "auth rejected (bad key)",
    402: "out of credit / billing dead — treated as unavailable",
    403: "auth rejected (bad key)",
}


def _display(provider: str) -> str:
    """"openrouter:gemma" -> "Openrouter gemma". A rung's name is an id, not a label."""
    return provider.replace(":", " ").capitalize()


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


async def _report_free_roster() -> None:
    """Advisory only: is every pinned `:free` id still listed upstream?

    Deliberately non-fatal and deliberately silent when no OpenRouter key is set. A
    retired id does not fail the preflight — the chain simply falls through to another
    rung — but it *is* the kind of failure that stays invisible for weeks because a
    fallback silently absorbs it, so it gets printed. Everything, including reading
    settings, is inside the guard: an advisory must not be able to fail a run.
    """
    try:
        check = await check_free_model_roster(get_settings())
    except Exception as exc:  # noqa: BLE001
        print(f"Free-model roster: check failed ({exc})")
        return
    if not check.configured:
        return
    # `listed == 0` with no error means the catalogue was never consulted, i.e. no
    # OpenRouter key. The ids are still "configured" (they are the pinned defaults) so
    # this cannot be inferred from `configured`, and a run with no OpenRouter key should
    # not be told about a roster it does not use.
    if not check.listed and not check.error:
        return
    print(f"Free-model roster: {'OK' if check.ok else 'ADVISORY'}")
    print(check.render())


async def run_llm_preflight_or_fail() -> None:
    """Fail the run only when NO provider is usable; degrade loudly when some are.

    Mirrors ``LLMClient.chat_completion``, which tries Groq, falls through to Cerebras on
    any failure, and only gives up when both are down. A billing-dead Cerebras fallback
    (402) is exactly the case the run must survive: Groq is healthy, so the ingest
    continues and the dead provider is reported rather than fatal.
    """
    results = await run_llm_preflight()
    healthy = [_display(p) for p, r in results.items() if r["ok"]]
    degraded = {_display(p): _detail(r) for p, r in results.items() if not r["ok"]}

    print("LLM preflight status:")
    for provider, result in results.items():
        name = _display(provider)
        detail = result["status"] if result["ok"] else degraded[name]
        print(f"  {name:<22} {'OK' if result['ok'] else 'DEGRADED':<9} {detail}")

    await _report_free_roster()

    if not healthy:
        print("LLM preflight failed: no usable LLM provider")
        sys.exit(1)

    for name, detail in degraded.items():
        _warn(name, detail)

    lost = "; ".join(f"{name} unavailable ({detail})" for name, detail in degraded.items())
    print(f"LLM preflight OK: continuing on {', '.join(healthy)}" + (f"; {lost}" if lost else ""))
