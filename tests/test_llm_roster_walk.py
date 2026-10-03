"""Tests for the roster walk: ordering, fall-through, demotion, and per-rung accounting.

The three things this file exists to pin down are the three things the old
hand-written chains got wrong, and each one is a behaviour nobody would have written a
test for on purpose:

1. A 429 that survived its retries used to end the whole request with "No LLM provider
   available" while a working fallback sat unread in the next `if`. On a shared free
   pool that was every request.
2. Cerebras spent against no budget at all, so a Cerebras-only day was uncountable.
3. There was no way to remember that a rung was dead, so a locked-out rung was re-probed
   -- three requests and two backoffs -- for every article in the batch.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import HTTPStatusError, Request, Response

from src.shared.budget import (
    CEREBRAS_REQUESTS,
    GROQ_REQUESTS,
    OPENROUTER_GEMMA_REQUESTS,
    OPENROUTER_NEMOTRON_REQUESTS,
    today,
    used,
)
from tenacity import RetryError

from src.shared.llm import LLMClient, LLMError
from src.shared.llm_budget import unwrap_retry
from src.shared.llm_roster import ROSTER


def _client(groq=None, cerebras=None, openrouter_key=None):
    """An LLMClient with exactly the rungs the test names, and nothing else."""
    client = LLMClient()
    client.groq_client = groq
    client.cerebras_client = cerebras
    client._openrouter_key = openrouter_key
    return client


def _status_error(status: int, url: str = "https://api.test/v1/chat/completions",
                  headers: dict | None = None) -> HTTPStatusError:
    request = Request("POST", url)
    response = Response(status, request=request, headers=headers or {}, json={})
    return HTTPStatusError(str(status), request=request, response=response)


def _completion(content: str):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


MESSAGES = [{"role": "user", "content": "test"}]


class TestWalkOrder:
    @pytest.mark.asyncio
    async def test_groq_is_tried_before_cerebras(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.return_value = _completion("from groq")
        cerebras.chat.completions.create.return_value = _completion("from cerebras")

        result = await _client(groq, cerebras).chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "from groq"
        cerebras.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_free_rung_is_used_when_the_keyed_ones_are_absent(self):
        client = _client(openrouter_key="sk-or-test")
        with patch.object(
            LLMClient, "_chat_completion_openrouter", new=AsyncMock(
                return_value={"choices": [{"message": {"content": "from free"}}]}
            )
        ) as call:
            result = await client.chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "from free"
        # The first free rung, not both of them, and not the second.
        assert call.await_args.args[0].name == "openrouter:gemma"


class TestFallThrough:
    @pytest.mark.asyncio
    async def test_a_429_falls_through_to_the_next_rung(self):
        """The regression. The old chain re-raised this as a fatal LLMError with a healthy
        Cerebras right below it."""
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(429)
        cerebras.chat.completions.create.return_value = _completion("from cerebras")

        result = await _client(groq, cerebras).chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "from cerebras"
        assert groq.chat.completions.create.call_count == 3  # retried, then given up on

    @pytest.mark.asyncio
    async def test_a_5xx_falls_through_to_the_next_rung(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(503)
        cerebras.chat.completions.create.return_value = _completion("from cerebras")

        result = await _client(groq, cerebras).chat_completion(MESSAGES)
        assert result["choices"][0]["message"]["content"] == "from cerebras"

    @pytest.mark.asyncio
    async def test_the_walk_does_not_stop_at_the_first_dead_rung(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(500)
        cerebras.chat.completions.create.side_effect = _status_error(401)

        with pytest.raises(LLMError, match="No LLM provider available"):
            await _client(groq, cerebras).chat_completion(MESSAGES)
        assert groq.chat.completions.create.call_count == 3
        assert cerebras.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_a_rate_limited_roster_raises_rather_than_degrading(self):
        """Returning 0.5 for every article in a batch because the provider was busy turns a
        transient blip into a day of fabricated neutrality that no error surfaces."""
        groq = AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(429)
        client = _client(groq, None)

        with pytest.raises(LLMError, match="No LLM provider available"):
            await client.classify_relevance("title", "body")

    @pytest.mark.asyncio
    async def test_a_rejected_key_degrades_to_the_default_score(self):
        """The opposite case: a bad key is not coming back within the run, so degrading is
        correct and raising would fail a run that could still curate."""
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)
        cerebras.chat.completions.create.side_effect = _status_error(403)

        assert await _client(groq, cerebras).classify_relevance("t", "b") == 0.5

    @pytest.mark.asyncio
    async def test_no_provider_at_all_degrades_rather_than_raising(self):
        assert await _client(None, None).classify_relevance("t", "b") == 0.5
        assert await _client(None, None).generate_caption("s", ["f"], []) is None

    @pytest.mark.asyncio
    async def test_chat_completion_raises_when_there_is_no_provider(self):
        with pytest.raises(LLMError, match="No LLM provider available"):
            await _client(None, None).chat_completion(MESSAGES)


class TestOutputRejection:
    @pytest.mark.asyncio
    async def test_a_rejected_caption_falls_through(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.return_value = _completion(
            "the quick brown fox jumps over the lazy dog and keeps on running for a while"
        )
        cerebras.chat.completions.create.return_value = _completion("Short and clean.")

        with patch("src.shared.llm.validate_caption", side_effect=[(False, "n-gram overlap"), (True, "")]):
            caption = await _client(groq, cerebras).generate_caption("s", ["f"], [])

        assert caption == "Short and clean."

    @pytest.mark.asyncio
    async def test_a_rejection_does_not_demote_the_rung(self):
        """One malformed response says nothing about the next article, and demoting on it
        would let a single bad reply walk the batch off the primary provider."""
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion("verbatim source text here")
        client = _client(groq, None)

        with patch("src.shared.llm.validate_caption", return_value=(False, "n-gram overlap")):
            assert await client.generate_caption("s", ["f"], []) is None

        assert client._demoted == set()
        assert "groq" not in client._demoted

    @pytest.mark.asyncio
    async def test_an_unparseable_score_falls_through(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.return_value = _completion("not json at all")
        cerebras.chat.completions.create.return_value = _completion('{"score": 0.9}')

        assert await _client(groq, cerebras).classify_relevance("t", "b") == 0.9


class TestDemotion:
    @pytest.mark.asyncio
    async def test_a_locked_out_rung_is_not_reprobed_for_every_article(self):
        """The reason demotion exists. Without it, each article pays three 429s and two
        backoffs to rediscover a fact the process already learned."""
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(429)
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)

        await client.chat_completion(MESSAGES)
        first = groq.chat.completions.create.call_count
        await client.chat_completion([{"role": "user", "content": "another"}])
        second = groq.chat.completions.create.call_count

        assert first == 3
        assert second == first, "the dead rung was probed again"

    @pytest.mark.asyncio
    async def test_a_rejected_key_demotes_the_rung(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)

        await client.chat_completion(MESSAGES)
        assert client._demoted == {"groq"}
        assert client._pinned == "cerebras"

    @pytest.mark.asyncio
    async def test_the_pinned_rung_is_tried_first_afterwards(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)

        await client.chat_completion(MESSAGES)
        # Groq recovers, but the run is pinned to what actually worked.
        groq.chat.completions.create.side_effect = None
        groq.chat.completions.create.return_value = _completion("from groq")

        await client.chat_completion([{"role": "user", "content": "second"}])
        assert client._pinned == "cerebras"

    @pytest.mark.asyncio
    async def test_demotion_is_per_client_not_global(self):
        """Two clients in one process must not inherit each other's dead rungs, or one
        unlucky client poisons the others for the rest of the process."""
        groq = AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)
        dead = _client(groq, None)
        fresh = _client(AsyncMock(), None)

        assert dead._demoted == set()
        await dead.classify_relevance("t", "b")
        assert dead._demoted == {"groq"}
        assert fresh._demoted == set()
        assert len(fresh._candidates()) == 1

    @pytest.mark.asyncio
    async def test_every_rung_demoted_leaves_nothing_to_try(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)
        cerebras.chat.completions.create.side_effect = _status_error(403)
        client = _client(groq, cerebras)

        with pytest.raises(LLMError):
            await client.chat_completion(MESSAGES)
        assert client._demoted == {"groq", "cerebras"}
        assert client._candidates() == []


class TestAuthWarning:
    @pytest.mark.asyncio
    async def test_one_annotation_per_rung_per_run(self, capsys):
        groq, cerebras = AsyncMock(), AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)
        cerebras.chat.completions.create.side_effect = _status_error(403)
        client = _client(groq, cerebras)

        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            with pytest.raises(LLMError):
                await client.chat_completion(MESSAGES)
            with pytest.raises(LLMError):
                await client.chat_completion(MESSAGES)

        out = capsys.readouterr().out
        assert out.count("::warning title=LLM provider auth failed::") == 2
        assert "Groq authentication failed (401/403)" in out
        assert "Cerebras authentication failed (401/403)" in out

    @pytest.mark.asyncio
    async def test_no_annotation_outside_ci(self, capsys):
        groq = AsyncMock()
        groq.chat.completions.create.side_effect = _status_error(401)

        with patch.dict("os.environ", {}, clear=True):
            await _client(groq, None).classify_relevance("t", "b")

        assert "::warning" not in capsys.readouterr().out


class TestPerRungBudgets:
    @pytest.mark.asyncio
    async def test_groq_spends_the_groq_row(self):
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion("ok")
        client = _client(groq, None)

        await client.chat_completion(MESSAGES)

        assert await used(GROQ_REQUESTS, day=today()) == 1
        assert await used(CEREBRAS_REQUESTS, day=today()) == 0

    @pytest.mark.asyncio
    async def test_cerebras_spends_its_own_row(self):
        """The gap: Cerebras previously spent against nothing at all, so its traffic was
        both uncountable and uncapped."""
        cerebras = AsyncMock()
        cerebras.chat.completions.create.return_value = _completion("ok")
        client = _client(None, cerebras)

        await client.chat_completion(MESSAGES)

        assert await used(CEREBRAS_REQUESTS, day=today()) == 1
        assert await used(GROQ_REQUESTS, day=today()) == 0

    @pytest.mark.asyncio
    async def test_each_free_model_spends_its_own_row(self):
        """One row per model, not per provider: a shared row would let Gemma's outage
        report itself as Nemotron's exhausted budget.

        Mocked at the httpx layer rather than at _chat_completion_openrouter, because
        patching the transport would skip the budget and prove nothing about it.
        """
        client = _client(openrouter_key="sk-or-test")
        client.settings.openrouter_base_url = "https://openrouter.test/api/v1"
        client.settings.openrouter_timeout = 5.0

        async def _post(url, json=None, **kwargs):
            if "gemma" in json["model"]:
                raise _status_error(429, url="https://openrouter.test/api/v1/chat/completions",
                                    headers={"retry-after": "0"})
            return Response(200, request=Request("POST", "https://openrouter.test/api/v1/chat/completions"),
                            json={"choices": [{"message": {"content": "ok"}}]})

        client._openrouter_http = AsyncMock()
        client._openrouter_http.post.side_effect = _post

        result = await client.chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "ok"
        assert await used(OPENROUTER_GEMMA_REQUESTS, day=today()) == 3
        assert await used(OPENROUTER_NEMOTRON_REQUESTS, day=today()) == 1

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_demotes_and_falls_through(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)
        client._budget_for(ROSTER[0]).daily_limit = 0

        result = await client.chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "from cerebras"
        groq.chat.completions.create.assert_not_called()
        assert "groq" in client._demoted

    @pytest.mark.asyncio
    async def test_a_budget_skipped_rung_is_recorded_in_stats(self):
        groq, cerebras = AsyncMock(), AsyncMock()
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)
        client._budget_for(ROSTER[0]).daily_limit = 0

        from src.utils.ingest_stats import STATS

        # A delta, not an absolute: STATS is a process-wide singleton, so the exact count
        # depends on what other tests have already recorded.
        before = STATS.snapshot().get("groq.budget_skipped", 0)
        await client.chat_completion(MESSAGES)
        assert STATS.snapshot().get("groq.budget_skipped", 0) == before + 1


class TestPreflightVisibility:
    @pytest.mark.asyncio
    async def test_a_free_rung_is_probed_through_its_own_transport(self):
        """Probing it through chat_completion would let a healthy Groq answer for it, which
        is the exact bug this preflight was written to fix."""
        from src.shared.llm_preflight import run_llm_preflight

        client = MagicMock()
        client._chat_completion_groq = AsyncMock()
        client._chat_completion_cerebras = AsyncMock()
        client._chat_completion_openrouter = AsyncMock(
            return_value={"choices": [{"message": {"content": "pong"}}]}
        )
        client.chat_completion = AsyncMock()
        settings = MagicMock(
            groq_api_key="", cerebras_api_key="", groq_model="", cerebras_model="",
            openrouter_api_key="sk-or-test", openrouter_gemma_model="",
            openrouter_nemotron_model="", openrouter_base_url="https://x.test/v1",
        )

        with patch("src.shared.llm_preflight.get_llm_client", new=AsyncMock(return_value=client)), \
             patch("src.shared.llm_preflight.get_settings", return_value=settings):
            results = await run_llm_preflight()

        assert results["openrouter:gemma"]["ok"] is True
        assert results["openrouter:nemotron"]["ok"] is True
        assert results["groq"]["configured"] is False
        client._chat_completion_openrouter.assert_awaited()
        client.chat_completion.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_free_rung_401_is_not_masked_by_a_healthy_groq(self):
        from src.shared.llm_preflight import run_llm_preflight

        client = MagicMock()
        client._chat_completion_groq = AsyncMock(
            return_value={"choices": [{"message": {"content": "pong"}}]}
        )
        client._chat_completion_cerebras = AsyncMock()
        client._chat_completion_openrouter = AsyncMock(side_effect=Exception("Error code: 401"))
        settings = MagicMock(
            groq_api_key="gsk_test", cerebras_api_key="", groq_model="m", cerebras_model="",
            openrouter_api_key="sk-or-test", openrouter_gemma_model="",
            openrouter_nemotron_model="", openrouter_base_url="https://x.test/v1",
        )

        with patch("src.shared.llm_preflight.get_llm_client", new=AsyncMock(return_value=client)), \
             patch("src.shared.llm_preflight.get_settings", return_value=settings):
            results = await run_llm_preflight()

        assert results["groq"]["ok"] is True
        assert results["openrouter:gemma"]["ok"] is False
        assert results["openrouter:gemma"]["status"] == 401


class TestOpenRouterTransport:
    @pytest.mark.asyncio
    async def test_an_empty_completion_is_a_failure_not_a_success(self):
        """The reasoning models on the free tier spend a small max_tokens budget on
        reasoning and return nothing. Handing that to a caller as a caption would be a
        caption of ''."""
        client = _client(openrouter_key="sk-or-test")
        client.settings.openrouter_base_url = "https://openrouter.test/api/v1"
        client.settings.openrouter_timeout = 5.0

        response = Response(200, request=Request("POST", "https://openrouter.test/api/v1/chat/completions"),
                            json={"choices": [{"message": {"content": ""}}]})
        http = AsyncMock()
        http.post.return_value = response
        client._openrouter_http = http

        with pytest.raises(LLMError, match="empty completion"):
            await client._chat_completion_openrouter(
                ROSTER[1], MESSAGES, max_tokens=8, temperature=0
            )

    @pytest.mark.asyncio
    async def test_a_rate_limit_raises_an_http_error_carrying_retry_after(self):
        """The backoff can only honour Retry-After if the error it retries on still has the
        response attached.

        retry-after is 0 here on purpose: honouring a 42 for real would make this test take
        84 seconds, which is a test nobody runs. That the value is honoured rather than
        ignored is proved in tests/test_llm_budget_limits.py and, live, in the report.
        """
        client = _client(openrouter_key="sk-or-test")
        client.settings.openrouter_base_url = "https://openrouter.test/api/v1"
        client.settings.openrouter_timeout = 5.0

        error = _status_error(429, url="https://openrouter.test/api/v1/chat/completions",
                              headers={"retry-after": "0"})
        http = AsyncMock()
        http.post.side_effect = error
        client._openrouter_http = http

        with pytest.raises(RetryError) as exc_info:
            await client._chat_completion_openrouter(ROSTER[1], MESSAGES, max_tokens=8, temperature=0)

        # Three attempts, then the wrapper -- so the walk has to unwrap to see the status.
        assert http.post.call_count == 3
        inner = unwrap_retry(exc_info.value)
        assert isinstance(inner, HTTPStatusError)
        assert inner.response.status_code == 429
        assert "retry-after" in inner.response.headers

    @pytest.mark.asyncio
    async def test_no_key_means_the_transport_refuses(self):
        client = _client(openrouter_key=None)
        with pytest.raises(LLMError, match="not initialized"):
            await client._chat_completion_openrouter(ROSTER[1], MESSAGES)

    @pytest.mark.asyncio
    async def test_close_closes_the_openrouter_client(self):
        client = _client(openrouter_key="sk-or-test")
        http = AsyncMock()
        client._openrouter_http = http
        await client.close()
        http.aclose.assert_awaited_once()
        assert client._openrouter_http is None
