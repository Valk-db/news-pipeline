"""Tests for LLM preflight check."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.shared.llm_preflight import run_llm_preflight, run_llm_preflight_or_fail

OK_RESPONSE = {"choices": [{"message": {"content": "pong"}}]}


def _client(groq=None, cerebras=None):
    """Mock LLMClient exposing the two per-provider entry points the preflight must use."""
    client = MagicMock()
    client._chat_completion_groq = groq or AsyncMock(return_value=OK_RESPONSE)
    client._chat_completion_cerebras = cerebras or AsyncMock(return_value=OK_RESPONSE)
    # The fallback-aware entry point must never be used to probe a specific provider.
    client.chat_completion = AsyncMock(return_value=OK_RESPONSE)
    return client


def _http_error(status: int) -> Exception:
    error = Exception(f"Error code: {status}")
    error.status_code = status  # type: ignore[attr-defined]
    return error


def _settings(groq="gsk_test", cerebras="csk_test"):
    return MagicMock(
        groq_api_key=groq, cerebras_api_key=cerebras,
        groq_model="groq-model", cerebras_model="cerebras-model",
    )


async def _run(client, settings):
    with patch("src.shared.llm_preflight.get_llm_client", new_callable=AsyncMock) as factory, \
         patch("src.shared.llm_preflight.get_settings", return_value=settings):
        factory.return_value = client
        return await run_llm_preflight()


@pytest.mark.asyncio
async def test_preflight_groq_200_cerebras_200():
    client = _client()
    results = await _run(client, _settings())

    assert results["groq"] == {"status": 200, "ok": True}
    assert results["cerebras"] == {"status": 200, "ok": True}
    client._chat_completion_groq.assert_awaited_once()
    client._chat_completion_cerebras.assert_awaited_once()
    client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_groq_401_is_not_masked_by_working_cerebras():
    """Regression: probing via chat_completion() fell back to Cerebras and reported Groq healthy."""
    client = _client(groq=AsyncMock(side_effect=_http_error(401)))
    results = await _run(client, _settings())

    assert results["groq"]["ok"] is False
    assert results["groq"]["status"] == 401
    assert results["cerebras"] == {"status": 200, "ok": True}
    client.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_cerebras_401_is_reported_even_when_groq_works():
    """Regression: the old 'cerebras' probe re-tested Groq, so a bad Cerebras key never showed."""
    client = _client(cerebras=AsyncMock(side_effect=_http_error(401)))
    results = await _run(client, _settings())

    assert results["groq"] == {"status": 200, "ok": True}
    assert results["cerebras"]["ok"] is False
    assert results["cerebras"]["status"] == 401


@pytest.mark.asyncio
async def test_preflight_unwraps_tenacity_retry_error():
    inner = _http_error(429)
    retry_error = Exception("RetryError[<Future at 0x1 state=finished raised Exception>]")
    retry_error.last_attempt = MagicMock()  # type: ignore[attr-defined]
    retry_error.last_attempt.exception.return_value = inner  # type: ignore[attr-defined]
    client = _client(groq=AsyncMock(side_effect=retry_error))

    results = await _run(client, _settings(cerebras=""))

    assert results["groq"]["status"] == 429
    assert results["groq"]["ok"] is False


@pytest.mark.asyncio
async def test_preflight_status_falls_back_to_message_then_500():
    client = _client(groq=AsyncMock(side_effect=Exception("401 Unauthorized")),
                     cerebras=AsyncMock(side_effect=Exception("connection reset")))
    results = await _run(client, _settings())

    assert results["groq"]["status"] == 401
    assert results["cerebras"]["status"] == 500


@pytest.mark.asyncio
async def test_preflight_no_keys():
    client = _client()
    results = await _run(client, _settings(groq="", cerebras=""))

    assert results["groq"]["ok"] is False
    assert results["groq"]["configured"] is False
    assert results["groq"]["error"] == "No API key configured"
    assert results["cerebras"]["ok"] is False
    assert results["cerebras"]["configured"] is False
    client._chat_completion_groq.assert_not_called()
    client._chat_completion_cerebras.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_or_fail_success():
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 200, "ok": True},
            "cerebras": {"status": 200, "ok": True},
        }
        await run_llm_preflight_or_fail()  # must not raise SystemExit


@pytest.mark.asyncio
async def test_preflight_or_fail_failure():
    """A configured provider that is rejected fails the run."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 401, "ok": False, "error": "Unauthorized"},
            "cerebras": {"status": 200, "ok": True},
        }
        with pytest.raises(SystemExit) as exc_info:
            await run_llm_preflight_or_fail()
        assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_preflight_or_fail_optional_backup_not_configured_is_fine():
    """README: Cerebras is an optional backup. An unset key must not fail the run."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 200, "ok": True},
            "cerebras": {"status": None, "ok": False, "configured": False, "error": "No API key configured"},
        }
        await run_llm_preflight_or_fail()  # must not raise SystemExit


@pytest.mark.asyncio
async def test_preflight_or_fail_no_provider_configured():
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": None, "ok": False, "configured": False, "error": "No API key configured"},
            "cerebras": {"status": None, "ok": False, "configured": False, "error": "No API key configured"},
        }
        with pytest.raises(SystemExit) as exc_info:
            await run_llm_preflight_or_fail()
        assert exc_info.value.code == 1