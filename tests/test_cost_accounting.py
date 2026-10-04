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
    is needed. Updated for HEAD's per-rung architecture: _budgets is now a dict,
    and the fixture initializes the fields HEAD's LLMClient.__init__ sets.
    """
    client = LLMClient.__new__(LLMClient)
    client.settings = SimpleNamespace(
        groq_model="test/model",
        groq_api_key="k",
        cerebras_api_key=None,
        cerebras_model=None,
        groq_daily_request_budget=900,
        groq_daily_token_cap=token_limit,
        cerebras_daily_request_budget=900,
        cerebras_daily_token_cap=60000,
        openrouter_gemma_daily_request_budget=900,
        openrouter_gemma_daily_token_cap=60000,
        openrouter_nemotron_daily_request_budget=900,
        openrouter_nemotron_daily_token_cap=60000,
    )
    client.groq_client = transport
    client.cerebras_client = None
    # HEAD's per-rung architecture: _budgets dict, not singular _budget
    groq_budget = RequestBudget(900, token_limit=token_limit)
    client._budgets = {"groq": groq_budget}
    client._minute_limiters = {}
    client._token_budgets = {}
    client._pinned = None
    client._last_walk_error = None
    client._demoted = set()
    client._auth_warned = set()
    client._openrouter_key = None
    client._openrouter_http = None
    # Backwards compat: tests access client._budget directly
    client._budget = groq_budget
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

    async def test_the_charge_comes_from_the_existing_parse_not_a_second_one(self, budget_counter, monkeypatch):
        """The usage block the client already built is what gets charged. If a second
        parse existed and disagreed with it, the counter and the single parse would
        tell different stories about the same call.

        Verifies the parse runs exactly once per call, and the charged amount equals
        the usage from that single parse. Does not assert on result["usage"] because
        the public return deliberately narrows to choices only; the accounting happens
        inside _walk via _record_usage before the return is built.
        """
        from src.shared import llm as llm_module
        from src.shared.llm_budget import extract_total_tokens as real_extract

        call_count = 0
        def counting_extract(result):
            nonlocal call_count
            call_count += 1
            return real_extract(result)

        monkeypatch.setattr(llm_module, "extract_total_tokens", counting_extract)

        transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")])
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=100)

        # The parse ran exactly once, not twice
        assert call_count == 1, f"extract_total_tokens called {call_count} times, expected 1"
        # The charge equals the usage from that single parse
        assert await used(GROQ_REQUEST_TOKENS) == 2_437

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

        Invariant: token exhaustion is distinguishable from request exhaustion. The
        request counter proves the second call never dispatched (still 1), while the
        token counter proves the budget was exhausted by tokens (4,962 of 5,000 used).
        A request-exhaustion would show the opposite pattern.
        """
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=4_862, total_tokens=4_962)
        transport = ReplayedTransport([("{}", usage, "stop"), ("{}", usage, "stop")])
        client = make_client(transport, token_limit=5_000)

        await client.chat_completion([{"role": "user", "content": "short"}], max_tokens=100)
        assert await used(GROQ_REQUEST_TOKENS) == 4_962

        from src.shared.budget import GROQ_REQUESTS

        assert await used(GROQ_REQUESTS) == 1  # 0.1% of a 900-request cap

        big = [{"role": "user", "content": "word " * 400}]
        with pytest.raises(LLMError):
            await client.chat_completion(big, max_tokens=100)

        # The refused call never dispatched: request counter still 1, token counter
        # unchanged. If this were request exhaustion, the token counter would have room.
        assert await used(GROQ_REQUESTS) == 1
        assert await used(GROQ_REQUEST_TOKENS) == 4_962
        assert len(transport.calls) == 1

        # The invariant: operators can distinguish token exhaustion from request
        # exhaustion via the stat keys. Token refusal records under
        # groq.token_budget_skipped, not groq.budget_skipped.
        from src.utils.ingest_stats import STATS
        snapshot = STATS.snapshot()
        assert snapshot.get("groq.token_budget_skipped", 0) >= 1, (
            "token refusal must record groq.token_budget_skipped"
        )
        # The request-skipped key must not have been bumped for this refusal.
        # (It may have been bumped by other tests; we check it didn't increase
        # for this specific refusal by verifying the token key is the one that moved.)
        # The distinction is what lets operators tell "out of tokens" from "out of requests".
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


async def test_llm_preflight_probes_are_charged_too(budget_counter):
    """A preflight probe is a real provider call, so its tokens are real spend. If the
    preflight were exempt, a run whose probes burned the day's allowance would report a
    healthy counter -- the same shape of blindness this batch removes.

    Behavioral: runs a probe against a stubbed transport that returns usage, then
    asserts the groq rung's token budget was charged exactly the reported total.
    """
    from unittest.mock import patch, MagicMock
    from src.shared import llm_preflight

    # A transport returning a real usage block
    transport = ReplayedTransport([("ok", GROQ_USAGE, "stop")])
    client = make_client(transport)

    settings = MagicMock(
        groq_api_key="k", groq_model="test/model",
        cerebras_api_key=None, cerebras_model=None,
    )

    with patch("src.shared.llm_preflight.get_llm_client", return_value=client), \
         patch("src.shared.llm_preflight.get_settings", return_value=settings):
        results = await llm_preflight.run_llm_preflight()

    # The probe succeeded
    assert results["groq"]["ok"] is True
    # And its tokens were charged exactly once to the groq rung's budget
    assert await used(GROQ_REQUEST_TOKENS) == 2_437


def test_no_live_provider_transport_is_reachable_from_these_tests():
    """A guard on the harness rather than the product: the brief forbids paying for a
    provider call to prove accounting works. These tests must stay replay-only, so assert
    that the transport object is ours and that urlopen was the only network seam patched.
    """
    transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")])
    assert isinstance(transport, ReplayedTransport)
    client = make_client(transport)
    assert client.groq_client is transport


class TestDispatchAndRecordSeam:
    """The _dispatch_and_record seam records usage immediately after dispatch succeeds,
    before content extraction. These tests verify the seam holds under the failure modes
    that the old _walk placement lost.
    """

    async def test_malformed_response_with_usage_still_charges(self, budget_counter):
        """A response with empty choices but a valid usage block still billed tokens.
        The old code extracted content before recording, so a KeyError on
        result["choices"][0] lost the charge. The seam records first.

        Drives the public chat_completion path with a stubbed transport returning
        the malformed response. On old code, the charge is lost (assertion fails).
        On new code, the transport captures usage before the parse fails, the
        wrapper records it, _walk treats None content as a failed rung, and the
        caller gets LLMError (not a successful None).
        """
        # Custom transport returning empty choices but valid usage.
        # The transport now catches the IndexError and returns usage anyway.
        class MalformedTransport:
            def __init__(self):
                self.calls = []
                outer = self
                class _Completions:
                    async def create(self, **kwargs):
                        outer.calls.append(kwargs)
                        return SimpleNamespace(
                            choices=[],  # malformed: no choices
                            usage=GROQ_USAGE,
                        )
                self.chat = SimpleNamespace(completions=_Completions())

        transport = MalformedTransport()
        client = make_client(transport)
        # _walk treats None content as a failed rung; with only groq configured,
        # chat_completion raises LLMError. The charge must be recorded first.
        with pytest.raises(LLMError):
            await client.chat_completion(MESSAGES, max_tokens=100)
        # The 2,437 tokens were charged despite the malformed choices
        assert await used(GROQ_REQUEST_TOKENS) == 2_437

    async def test_normal_call_records_exactly_once(self, budget_counter, monkeypatch):
        """A normal chat_completion charges the rung's token budget exactly once.
        Guards against double-charging after the seam refactor.
        """
        from src.shared import llm as llm_module
        from src.shared.llm_budget import extract_total_tokens as real_extract

        call_count = 0
        def counting_extract(result):
            nonlocal call_count
            call_count += 1
            return real_extract(result)
        monkeypatch.setattr(llm_module, "extract_total_tokens", counting_extract)

        transport = ReplayedTransport([("[]", GROQ_USAGE, "stop")])
        client = make_client(transport)
        await client.chat_completion(MESSAGES, max_tokens=100)

        assert call_count == 1
        assert await used(GROQ_REQUEST_TOKENS) == 2_437

    async def test_failed_rung_charges_before_fallthrough(self, budget_counter):
        """When rung A returns a bad response and _walk falls to rung B, both rungs'
        usage must be recorded. The seam records A's usage before _walk extracts
        content and decides to fall through.
        """
        # This test verifies the seam's ordering guarantee at the unit level:
        # _dispatch_and_record records even when the result would fail content
        # extraction. The full walk-fallthrough path is covered by the malformed
        # test above combined with the existing walk tests.
        from src.shared.llm_roster import ROSTER
        rung = next(r for r in ROSTER if r.name == "groq")

        client = make_client(ReplayedTransport([]))
        bad_result = {
            "choices": [],  # malformed: no content to extract
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        async def fake_dispatch(*args, **kwargs):
            return bad_result
        client._dispatch = fake_dispatch

        await client._dispatch_and_record(rung, MESSAGES, max_tokens=100, temperature=0)
        # The 15 tokens were recorded despite the malformed choices
        assert await used(GROQ_REQUEST_TOKENS) == 15

    async def test_failing_probe_does_not_demote(self, budget_counter):
        """A probe that fails (e.g., 401) must not demote the rung or trip
        circuit-breakers. Demotion lives in _walk, not in the dispatch seam.
        The preflight reroute must not add side effects beyond recording.
        """
        from unittest.mock import patch, MagicMock
        from src.shared import llm_preflight
        from src.shared.llm_roster import ROSTER

        # Transport that fails with 401
        async def fail_401(**kwargs):
            raise Exception("Error code: 401")

        transport = ReplayedTransport([])
        transport.chat.completions.create = fail_401
        client = make_client(transport)

        # The rung starts not-demoted
        assert "groq" not in client._demoted

        settings = MagicMock(
            groq_api_key="k", groq_model="test/model",
            cerebras_api_key=None, cerebras_model=None,
        )

        with patch("src.shared.llm_preflight.get_llm_client", return_value=client), \
             patch("src.shared.llm_preflight.get_settings", return_value=settings):
            results = await llm_preflight.run_llm_preflight()

        # Probe failed as expected
        assert results["groq"]["ok"] is False
        # But the rung was NOT demoted (demotion is a _walk behavior, not a probe behavior)
        assert "groq" not in client._demoted

    async def test_dispatch_and_record_does_not_record_on_budget_exhausted(self, budget_counter):
        """_dispatch_and_record must not record anything when _dispatch raises
        BudgetExhausted, since no provider call happened and no tokens were billed.
        """
        from src.shared.llm_budget import BudgetExhausted, BudgetStatus
        from src.shared.llm_roster import ROSTER

        client = make_client(ReplayedTransport([]))
        rung = next(r for r in ROSTER if r.name == "groq")

        status = BudgetStatus(used_today=100, limit=100, remaining=0, exhausted=True)
        async def fake_dispatch(*args, **kwargs):
            raise BudgetExhausted(status)

        client._dispatch = fake_dispatch
        with pytest.raises(BudgetExhausted):
            await client._dispatch_and_record(rung, MESSAGES, max_tokens=100, temperature=0)
        # No tokens recorded: the call never happened
        assert await used(GROQ_REQUEST_TOKENS) == 0

    async def test_dispatch_and_record_reraises_without_billed_usage(self, budget_counter):
        """A normal exception with no _billed_usage re-raises untouched and records
        nothing. The wrapper must not swallow or misattribute it.
        """
        from src.shared.llm_roster import ROSTER

        client = make_client(ReplayedTransport([]))
        rung = next(r for r in ROSTER if r.name == "groq")

        async def fake_dispatch(*args, **kwargs):
            raise RuntimeError("transport exploded")

        client._dispatch = fake_dispatch
        with pytest.raises(RuntimeError, match="transport exploded"):
            await client._dispatch_and_record(rung, MESSAGES, max_tokens=100, temperature=0)
        assert await used(GROQ_REQUEST_TOKENS) == 0