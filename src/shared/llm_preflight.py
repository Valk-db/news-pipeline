"""LLM preflight check - one minimal call per provider at workflow start."""

from src.shared.llm import get_llm_client
from src.shared.config import get_settings


async def run_llm_preflight() -> dict:
    """Run a minimal LLM call for each provider to verify authentication.

    Returns:
        dict with provider status: {"groq": {"status": 200, "ok": True}, "cerebras": {"status": 401, "ok": False}}
    """
    settings = get_settings()
    client = await get_llm_client()

    results = {}

    # Test Groq
    if settings.groq_api_key:
        try:
            # Minimal chat completion to test auth
            await client.chat_completion(
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            results["groq"] = {"status": 200, "ok": True}
        except Exception as e:
            # Extract status code if available
            status = getattr(e, 'status_code', None) or getattr(e, 'response', None)
            if status and hasattr(status, 'status_code'):
                status = status.status_code
            else:
                # Try to parse from error message
                import re
                match = re.search(r'(\d{3})', str(e))
                status = int(match.group(1)) if match else 500
            results["groq"] = {"status": status, "ok": False, "error": str(e)[:100]}
    else:
        results["groq"] = {"status": None, "ok": False, "error": "No API key configured"}

    # Test Cerebras
    if settings.cerebras_api_key:
        try:
            await client.chat_completion(
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            results["cerebras"] = {"status": 200, "ok": True}
        except Exception as e:
            status = getattr(e, 'status_code', None) or getattr(e, 'response', None)
            if status and hasattr(status, 'status_code'):
                status = status.status_code
            else:
                import re
                match = re.search(r'(\d{3})', str(e))
                status = int(match.group(1)) if match else 500
            results["cerebras"] = {"status": status, "ok": False, "error": str(e)[:100]}
    else:
        results["cerebras"] = {"status": None, "ok": False, "error": "No API key configured"}

    return results


async def run_llm_preflight_or_fail() -> None:
    """Run preflight and exit with error if any configured provider fails auth."""
    import sys

    results = await run_llm_preflight()

    failed = []
    for provider, result in results.items():
        if not result["ok"]:
            failed.append(f"{provider}: status={result.get('status')}, error={result.get('error')}")

    if failed:
        print(f"LLM preflight failed: {'; '.join(failed)}")
        sys.exit(1)

    print("LLM preflight OK: all providers authenticated")
