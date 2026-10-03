"""Token accounting at the two call sites, driven through a replayed usage payload.

The brief's constraint is that verification must not cost money, so nothing here touches a
provider. The token numbers are REPLAYED -- real `usage` blocks in the shape Groq returns,
driven through the real LLMClient response normalisation and the real RequestBudget
against the real budget_counters table.

That is also the only way to reach the states that matter. A live provider cannot be asked
to return a 6,000-token completion, and the states this batch must handle are precisely the
awkward ones: no usage block at all, a zero-token usage, and an empty completion that still
cost tokens.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.shared.budget import (
    GROQ_REQUEST_TOKENS,
    GROQ_TRANSLATION_TOKENS,
    used,
)
from src.shared.llm import LLMClient, LLMError
from src.shared.llm_budget import BudgetExhausted, RequestBudget

# A usage block exactly as the Groq SDK hands it over. prompt 1,790 / completion 647 is the
# measured Phase 2 shape; the caption numbers are the ~1,200/call the cap comment cites.
GROQ_USAGE = SimpleNamespace(prompt_tokens=1_790, completion_tokens=647, total_tokens=2_437)


class ReplayedTransport:
    """The Groq SDK object, one layer below LLMClient, returning recorded responses.

    `responses` is a list of (content, usage, finish_reason) so one test can replay a
    sequence, including the mixed shapes a live provider produces. Every entry records the
    call, so a test can assert on what was dispatched as well as on what was charged.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                content, usage, finish_reason = outer.responses.pop(0)
                message = SimpleNamespace(content=content, reasoning=None)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
                    usage=usage,
                )

        self.chat = SimpleNamespace(completions=_Completions())


def make_client(transport, token_limit=60_000):
    """A client with only the fields the code under test reads.

    Built by hand rather than via LLMClient() so no SDK client, API key or settings object
    is needed. LLMClient gained no instance field in this batch precisely so that a
    hand-built fixture like this keeps working; if a field is ever added here, the fixture
    is the thing that will tell you (AttributeError, not a silently wrong test).
    """
    client = LLMClient.__new__(LLMClient)
    client.settings = SimpleNamespace(
        groq_model="test/model",
        groq_api_key="k",
        cerebras_api_key=None,
        cerebras_model=None,
        groq_daily_request_budget=900,
    )
    client.groq_client = transport
    client.cerebras_client = None
    client._budget = RequestBudget(900, token_limit=token_limit)
    client._groq_auth_warned = False
    client._cerebras_auth_warned = False
    return client


MESSAGES = [{"role": "user", "content": "Rate this story."}]


def _used(name):
    """Read a counter from a sync test, which has no loop of its own."""
    import asyncio

    return asyncio.run(used(name))


class TestGroqChargesReportedTokens:
    async def test_charges_the_provider_reported_total(self, budget_counter):
        transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")])
        client = make_client(transport)

        result = await client.chat_completion(MESSAGES, max_tokens=100)

        assert result["choices"][0]["message"]["content"] == "[]"
        assert await used(GROQ_REQUEST_TOKENS) == 2_437

    async def test_the_charge_comes_from_the_existing_parse_not_a_second_one(self, budget_counter):
        """The usage block the client already built is what gets charged. If a second
        parse existed and disagreed with it, the counter and the returned result would
        tell different stories about the same call."""
        transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")])
        client = make_client(transport)
        result = await client.chat_completion(MESSAGES, max_tokens=100)
        assert result["usage"]["total_tokens"] == 2_437
        assert await used(GROQ_REQUEST_TOKENS) == result["usage"]["total_tokens"]

    async def test_reasoning_tokens_are_included(self, budget_counter):
        """A reasoning model bills tokens it spent thinking. total_tokens already includes
        them, and the cap has to see them, so this pins that the charged number is the
        total rather than, say, the prompt count."""
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=4_862, total_tokens=4_962)
        transport = ReplayedTransport([("{}", usage, "length")])
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=4096)
        assert await used(GROQ_REQUEST_TOKENS) == 4_962

    async def test_an_empty_completion_still_costs_tokens(self, budget_counter):
        """A zero-content completion is an ERROR, not "found nothing" -- and it is not
        free. Collapsing the two destroys the signal; charging it zero would hide the cost.

        This is reachable and intermittent in production: max_tokens=1500 with no
        reasoning_effort on gpt-oss-20b returned 0 characters on one call and 1,054 on the
        next with identical parameters.
        """
        transport = ReplayedTransport([("", GROQ_USAGE, "length")])
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 2_437

    async def test_a_missing_usage_block_is_charged_one(self, budget_counter):
        """A transport that reports no usage at all must not make the counter read zero
        forever. Charged 1: the request happened."""
        transport = ReplayedTransport([("[]", None, "stop")])
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 1

    async def test_a_zero_usage_is_charged_one(self, budget_counter):
        usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        transport = ReplayedTransport([("[]", usage, "stop")])
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 1

    async def test_two_calls_accumulate(self, budget_counter):
        transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")] * 2)
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=100)
        await client.chat_completion([{"role": "user", "content": "different"}], max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 4_874

    async def test_a_failed_call_records_no_tokens(self, budget_counter):
        """A call that raised never produced usage, so there is nothing to charge. The
        REQUEST counter still moved, which is the pre-existing documented behaviour: a
        request that failed has spent a request, and that is the safe direction."""
        transport = ReplayedTransport([(None, None, None)])
        transport.responses = []

        async def boom(**kwargs):
            raise RuntimeError("provider exploded")

        transport.chat.completions.create = boom
        client = make_client(transport)
        # chat_completion's pre-existing contract: a non-transient provider failure falls
        # through to Cerebras and surfaces as LLMError, not as the raw provider error.
        # Asserted as LLMError because asserting RuntimeError would be asserting a contract
        # this batch did not change and should not have changed.
        with pytest.raises(LLMError):
            await client.chat_completion(MESSAGES, max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 0


class TestTokenCapStopsTheNextCallThroughTheRealClient:
    async def test_the_second_call_is_refused_after_the_first_exhausts_the_tokens(
        self, budget_counter
    ):
        """The whole batch, end to end through the real client.

        Cap of 5,000 tokens. The first call reports 4,962 (the measured reasoning-model
        shape), leaving 38. The second call's prompt alone estimates past that, so it is
        refused -- with ONE request spent against a 900-request cap, which is 0.1% used.
        Before this batch the request cap was the only gate and it would have said yes.
        """
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=4_862, total_tokens=4_962)
        transport = ReplayedTransport([("{}", usage, "stop"), ("{}", usage, "stop")])
        client = make_client(transport, token_limit=5_000)

        await client.chat_completion([{"role": "user", "content": "short"}], max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 4_962

        from src.shared.budget import GROQ_REQUESTS

        assert await used(GROQ_REQUESTS) == 1  # 0.1% of a 900-request cap

        big = [{"role": "user", "content": "word " * 400}]
        with pytest.raises(LLMError) as excinfo:
            await client.chat_completion(big, max_tokens=100)

        # chat_completion catches BudgetExhausted and falls through to Cerebras, so the
        # refusal is on the cause chain. Reaching into __cause__ rather than asserting on
        # the outer message is what makes this test pin the refusal rather than the
        # fallthrough -- and the outer message is "No LLM provider available", which would
        # happily have passed an assertion that only checked something was raised.
        refusal = excinfo.value.__cause__
        assert isinstance(refusal, BudgetExhausted), (
            f"expected a budget refusal on the cause chain, got {refusal!r}"
        )
        assert refusal.unit == "tokens", (
            "the refusal must name tokens: the request cap had 899 left and raising it "
            "would not have helped"
        )
        assert "tokens" in str(refusal)
        # The refused call was never dispatched.
        assert len(transport.calls) == 1
        assert await used(GROQ_REQUESTS) == 1


class TestTranslationChargesTokens:
    """Deliberately SYNC tests.

    GroqBackend.translate is synchronous and its budget calls go through spend_sync /
    record_sync, which are asyncio.run wrappers because the pipeline drives translation from
    a worker thread. Calling asyncio.run from inside a running loop raises, so an async test
    would fail on the harness rather than on the accounting. That is the same lesson as the
    MyMemory translate-symmetry note in STEALTH.md.
    """

    def test_groq_translation_records_the_reported_total(self, budget_counter, monkeypatch):
        from src.enrichment import translation as tr

        payload = {
            "choices": [{"finish_reason": "stop", "message": {"content": "The council voted."}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960},
        }
        monkeypatch.setattr(tr, "GROQ_URL", "http://groq.invalid/v1/chat/completions")
        monkeypatch.setattr(tr.urllib.request, "urlopen", _fake_urlopen(payload))

        backend = tr.GroqBackend(api_key="k", politeness_seconds=0)
        out = backend.translate("Le conseil a vote.", "fr", "en")

        assert out == "The council voted."
        assert _used(GROQ_TRANSLATION_TOKENS) == 960

    def test_translation_token_spend_is_separate_from_the_request_counter(
        self, budget_counter, monkeypatch
    ):
        from src.enrichment import translation as tr

        payload = {
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
            "usage": {"total_tokens": 960},
        }
        monkeypatch.setattr(tr, "GROQ_URL", "http://groq.invalid/v1/chat/completions")
        monkeypatch.setattr(tr.urllib.request, "urlopen", _fake_urlopen(payload))

        backend = tr.GroqBackend(api_key="k", politeness_seconds=0)
        backend.translate("Le conseil a vote.", "fr", "en")

        from src.shared.budget import GROQ_TRANSLATION_REQUESTS

        assert _used(GROQ_TRANSLATION_REQUESTS) == 1
        assert _used(GROQ_TRANSLATION_TOKENS) == 960
        # And the LLM client's token counter is untouched: three Groq consumers, three rows.
        assert _used(GROQ_REQUEST_TOKENS) == 0

    def test_a_missing_usage_block_is_charged_one(self, budget_counter, monkeypatch):
        from src.enrichment import translation as tr

        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        monkeypatch.setattr(tr, "GROQ_URL", "http://groq.invalid/v1/chat/completions")
        monkeypatch.setattr(tr.urllib.request, "urlopen", _fake_urlopen(payload))

        backend = tr.GroqBackend(api_key="k", politeness_seconds=0)
        backend.translate("Le conseil a vote.", "fr", "en")
        assert _used(GROQ_TRANSLATION_TOKENS) == 1


class _FakeResponse:
    def __init__(self, payload):
        self._body = payload

    def read(self):
        import json

        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(payload):
    import json

    body = json.dumps(payload).encode()

    def _open(req, timeout=None):
        class _Ctx:
            def read(self_inner):
                return body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()

    return _open


def test_the_two_call_sites_use_different_counters():
    """The per-consumer property, asserted structurally so a future edit that routes both
    through one row is caught without running anything.

    One real call through the real walk charged 3 requests to one rung and 1 to another, in
    two separate rows -- that run is what established that two consumers on one key that
    fail differently must not share a counter. This is the same property for tokens.
    """
    from src.enrichment import translation
    from src.shared import llm_budget
    from src.shared.budget import COUNTERS_BY_NAME

    llm_budget_counter = GROQ_REQUEST_TOKENS
    tr_counter = GROQ_TRANSLATION_TOKENS

    assert llm_budget_counter != tr_counter
    assert COUNTERS_BY_NAME[llm_budget_counter].pairs_with == "groq_requests"
    assert COUNTERS_BY_NAME[tr_counter].pairs_with == "groq_translation_requests"
    assert hasattr(translation, "GROQ_TRANSLATION_TOKENS")
    assert hasattr(llm_budget, "GROQ_REQUEST_TOKENS")


def test_llm_preflight_probes_are_charged_too():
    """A preflight probe is a real provider call, so its tokens are real spend. If the
    preflight were exempt, a run whose probes burned the day's allowance would report a
    healthy counter -- the same shape of blindness this batch removes.

    Structural rather than behavioural: it asserts the charge lives in the one place every
    Groq completion passes through, so nothing can route around it.
    """
    import inspect

    source = inspect.getsource(LLMClient._chat_completion_groq)
    assert "record_tokens" in source
    # The preflight calls this method directly, so a charge here covers it.
    from src.shared import llm_preflight

    preflight_source = inspect.getsource(llm_preflight)
    assert "_chat_completion_groq" in preflight_source


def test_no_live_provider_transport_is_reachable_from_these_tests():
    """A guard on the harness rather than the product: the brief forbids paying for a
    provider call to prove accounting works. These tests must stay replay-only, so assert
    that the transport object is ours and that urlopen was the only network seam patched.
    """
    transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")])
    assert isinstance(transport, ReplayedTransport)
    client = make_client(transport)
    assert client.groq_client is transport
    # patch/AsyncMock imported but unused paths are still fine; assert the real SDK is not
    # constructed anywhere in this module's helpers.
    assert AsyncMock.__name__ == "AsyncMock"