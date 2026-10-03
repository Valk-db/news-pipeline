"""Tests for LLM preflight check."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.shared.llm import LLMError
from src.shared.llm_preflight import (
    _detail,
    _reason,
    _report_free_roster,
    run_llm_preflight,
    run_llm_preflight_or_fail,
)
from src.shared.llm_roster import RosterCheck

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
async def test_preflight_or_fail_billing_dead_fallback_only_warns(capsys):
    """A 402 Cerebras (trial over, key still set) must not kill a healthy Groq run."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 200, "ok": True},
            "cerebras": {"status": 402, "ok": False, "error": "Error code: 402 - payment required"},
        }
        await run_llm_preflight_or_fail()  # must not raise SystemExit

    out = capsys.readouterr().out
    assert "continuing on Groq" in out
    assert "Cerebras unavailable (402 out of credit" in out
    assert "auth rejected" not in out


@pytest.mark.asyncio
async def test_preflight_or_fail_dead_primary_healthy_fallback_continues(capsys):
    """Groq is the preferred provider, not a requirement: Cerebras alone still runs."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 401, "ok": False, "error": "Unauthorized"},
            "cerebras": {"status": 200, "ok": True},
        }
        await run_llm_preflight_or_fail()  # must not raise SystemExit

    out = capsys.readouterr().out
    assert "continuing on Cerebras" in out
    assert "Groq unavailable (401 auth rejected (bad key))" in out


@pytest.mark.asyncio
async def test_preflight_or_fail_optional_backup_not_configured_is_fine(capsys):
    """README: Cerebras is an optional backup. An unset key must not fail the run."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 200, "ok": True},
            "cerebras": {"status": None, "ok": False, "configured": False, "error": "No API key configured"},
        }
        await run_llm_preflight_or_fail()  # must not raise SystemExit

    out = capsys.readouterr().out
    assert "Cerebras unavailable (not configured (optional fallback))" in out


@pytest.mark.asyncio
async def test_preflight_or_fail_warns_as_actions_annotation(monkeypatch, capsys):
    """The dead provider must be visible in the Actions UI, not just in the log."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 200, "ok": True},
            "cerebras": {"status": 402, "ok": False, "error": "Error code: 402"},
        }
        await run_llm_preflight_or_fail()

    out = capsys.readouterr().out
    assert "::warning title=LLM provider unavailable::Cerebras unavailable: 402 out of credit" in out


@pytest.mark.asyncio
async def test_preflight_or_fail_all_providers_dead_exits(capsys):
    """Nothing usable left: that is the one case worth failing the whole ingest for."""
    with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
        mock_preflight.return_value = {
            "groq": {"status": 401, "ok": False, "error": "Unauthorized"},
            "cerebras": {"status": 402, "ok": False, "error": "Error code: 402"},
        }
        with pytest.raises(SystemExit) as exc_info:
            await run_llm_preflight_or_fail()
        assert exc_info.value.code == 1

    out = capsys.readouterr().out
    assert "no usable LLM provider" in out
    assert "Groq" in out and "Cerebras" in out  # per-provider detail, not just the verdict


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


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"status": 402, "ok": False, "error": "payment required"}, "402 out of credit / billing dead"),
        ({"status": 401, "ok": False, "error": "Unauthorized"}, "401 auth rejected (bad key)"),
        ({"status": 403, "ok": False, "error": "Forbidden"}, "403 auth rejected (bad key)"),
        (
            {"status": None, "ok": False, "configured": False, "error": "No API key configured"},
            "not configured (optional fallback)",
        ),
        (
            {"status": 500, "ok": False, "error": "InternalServerError: boom"},
            "500 InternalServerError: boom",
        ),
    ],
)
def test_detail_classifies_failures(result, expected):
    assert expected in _detail(result)


def test_reason_402_is_billing_not_auth():
    """402 means the key works but the account is empty; calling that an auth failure sends
    operators chasing the wrong fix (rotate the key) instead of topping up."""
    assert _reason({"status": 402, "ok": False, "error": "payment required"}) == (
        "out of credit / billing dead — treated as unavailable"
    )
    assert "auth" not in _detail({"status": 402, "ok": False, "error": "payment required"})


class TestPingBudget:
    """A preflight probe must be a question the model cannot answer with nothing.

    Live 2026-10-03: with a bare "ping" prompt the healthy free model
    nvidia/nemotron-3-super-120b-a12b:free returned no content 3/4 times at 64 tokens
    and 2/4 at 200, and the preflight printed `DEGRADED 500 ... returned an empty
    completion` -- indistinguishable from a real outage, so an operator would have gone
    to debug a provider that was fine. Raising max_tokens did not fix it; changing the
    prompt did. The assertion below is on that mechanism, so the prompt cannot go back to
    a content-free utterance.
    """

    @pytest.mark.asyncio
    async def test_a_model_that_answers_only_a_directive_prompt_is_not_reported_unhealthy(self):
        from src.shared.llm_roster import ROSTER

        free = next(r for r in ROSTER if r.name == "openrouter:nemotron")
        asked = {}

        async def reasoning_model(rung, messages, max_tokens, temperature, response_format=None):
            # Emits nothing for a content-free utterance, exactly as the live model did.
            asked["prompt"] = messages[0]["content"]
            asked["max_tokens"] = max_tokens
            if len(messages[0]["content"].split()) < 3:
                raise LLMError(f"{free.model} returned an empty completion")
            return OK_RESPONSE

        client = _client()
        client._chat_completion_openrouter = AsyncMock(side_effect=reasoning_model)
        results = await _run(
            client,
            MagicMock(openrouter_api_key="k", groq_api_key="gsk_test", cerebras_api_key=""),
        )

        assert results["openrouter:nemotron"] == {"status": 200, "ok": True}, asked
        assert asked["max_tokens"] >= 32, "the free tiers need room for reasoning before the answer"


class TestFreeRosterAdvisory:
    """The roster check is an ADVISORY: it prints, it never decides, and it never raises."""

    @pytest.mark.asyncio
    async def test_silent_when_no_openrouter_key(self, monkeypatch, capsys):
        """No key => no free ids configured => nothing to check, and nothing to say."""
        monkeypatch.setattr("src.shared.llm_preflight.get_settings", lambda: MagicMock(openrouter_api_key=""))
        with patch(
            "src.shared.llm_roster.fetch_free_model_ids",
            new_callable=AsyncMock,
        ) as mock_fetch:
            await _report_free_roster()
        mock_fetch.assert_not_awaited()  # must not hit the network for a deployment that has no key
        assert "Free-model roster" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_stale_id_prints_advisory_and_does_not_raise(self, monkeypatch, capsys):
        monkeypatch.setattr("src.shared.llm_preflight.get_settings", lambda: MagicMock(openrouter_api_key="k"))
        check = RosterCheck(
            ok=False,
            configured=("google/gemma-4-26b-a4b-it:free",),
            listed=17,
            stale=("google/gemma-4-26b-a4b-it:free",),
        )
        with patch("src.shared.llm_preflight.check_free_model_roster", new_callable=AsyncMock, return_value=check):
            await _report_free_roster()

        out = capsys.readouterr().out
        assert "ADVISORY" in out
        assert "STALE" in out
        assert "google/gemma-4-26b-a4b-it:free" in out

    @pytest.mark.asyncio
    async def test_healthy_roster_prints_ok(self, monkeypatch, capsys):
        monkeypatch.setattr("src.shared.llm_preflight.get_settings", lambda: MagicMock(openrouter_api_key="k"))
        check = RosterCheck(ok=True, configured=("nvidia/nemotron-3-super-120b-a12b:free",), listed=17, stale=())
        with patch("src.shared.llm_preflight.check_free_model_roster", new_callable=AsyncMock, return_value=check):
            await _report_free_roster()
        assert "Free-model roster: OK" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_check_that_raises_is_swallowed(self, monkeypatch, capsys):
        """An advisory that can fail a run is not an advisory."""
        monkeypatch.setattr("src.shared.llm_preflight.get_settings", lambda: MagicMock(openrouter_api_key="k"))
        with patch(
            "src.shared.llm_preflight.check_free_model_roster",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network down"),
        ):
            await _report_free_roster()  # must not raise
        assert "check failed (network down)" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_stale_roster_does_not_fail_an_otherwise_healthy_run(self, monkeypatch, capsys):
        """The whole point of advisory: a retired id degrades, it does not exit 1."""
        monkeypatch.setattr("src.shared.llm_preflight.get_settings", lambda: MagicMock(openrouter_api_key="k"))
        stale = RosterCheck(ok=False, configured=("x:free",), listed=1, stale=("x:free",))
        with patch("src.shared.llm_preflight.run_llm_preflight", new_callable=AsyncMock) as mock_preflight:
            mock_preflight.return_value = {"groq": {"status": 200, "ok": True}}
            with patch(
                "src.shared.llm_preflight.check_free_model_roster",
                new_callable=AsyncMock,
                return_value=stale,
            ):
                await run_llm_preflight_or_fail()  # must not raise SystemExit
        out = capsys.readouterr().out
        assert "ADVISORY" in out
        assert "continuing on Groq" in out
