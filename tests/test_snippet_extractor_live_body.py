"""Tests that actually EXECUTE the snippet/claim extraction bodies.

tests/test_enrichment_pipeline.py patches extract_snippets_from_article out at
all eight of its call sites, so the whole LLM stage of the enrichment pipeline
was never executed by the suite. That is how `llm = get_llm_client()` (an
un-awaited coroutine, on a client with no `.chat` and no `.model`) survived
long enough for the stage to return [] for every article, forever, and how a
`STATS.record(..., count=...)` TypeError could sit inside the same try block
and convert a good extraction into [] as well.

These tests drive the real function bodies against a fake *transport* only:
the real LLMClient, the real `_chat_completion_groq` normalisation, the real
`_parse_json_response` fence-stripping, the real post-processing. Nothing here
patches `extract_snippets_from_article`, and nothing re-implements the prompt.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.enrichment import snippet_extractor
from src.enrichment.snippet_extractor import (
    SNIPPET_MAX_TOKENS,
    extract_snippets_from_article,
)
from src.reliability.fact_checker import extract_claims_from_article
from src.shared.llm import LLMClient
from src.shared.llm_budget import RequestBudget

FENCED_ARRAY = """```json
[
  {
    "text": "Council member Dana Reyes said the shortfall was worse than reported.",
    "type": "quote",
    "entities": ["Dana Reyes"],
    "confidence": 88,
    "position_estimate": 0.42
  }
]
```"""

ARTICLE = (
    "The city council voted on Tuesday to delay the transit bond after a "
    "consulting firm found the projected cost had doubled. Council member "
    "Dana Reyes said the shortfall was worse than reported. "
) + "Filler sentence about the vote. " * 12


class FakeTransport:
    """Stands in for the Groq SDK object, one layer below LLMClient.

    Records what production asked for, so a test can assert on the wire
    contract (max_tokens, temperature) rather than on retyped constants.
    """

    def __init__(self, content: str = FENCED_ARRAY, raise_exc: Exception | None = None):
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.raise_exc is not None:
                    raise outer.raise_exc
                message = SimpleNamespace(content=outer.content, reasoning=None)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message, finish_reason="stop")],
                    usage=SimpleNamespace(
                        prompt_tokens=10, completion_tokens=20, total_tokens=30
                    ),
                )

        self.chat = SimpleNamespace(completions=_Completions())


class RecordingSpend:
    """Stands in for the budget_counters row. The budget itself is tested elsewhere.

    NOTE: an earlier hand-rolled `run(self, fn, *args, **kwargs)` fake was the wrong
    shape -- RequestBudget.run is `run(self, coro_factory, messages, model)` and calls
    `coro_factory()` with NO arguments (llm_budget.py:62-80). The fakesy version raised
    TypeError inside the transport, which the fail-soft handler turned into [], so 9
    tests asserted against a product that was never actually called. This uses the
    REAL RequestBudget and patches only the counter read.
    """

    def __init__(self):
        self.spends = 0

    async def __call__(self, key, n, limit):
        self.spends += 1
        return 1


SPEND = RecordingSpend()


@pytest.fixture(autouse=True)
def _no_budget_db(monkeypatch):
    """The real RequestBudget, with only the budget_counters row faked out.

    Patching llm_budget.spend (not RequestBudget) keeps the real reserve-then-dispatch
    order and the real in-flight coalescing in the path these tests exercise.
    """
    SPEND.spends = 0
    monkeypatch.setattr("src.shared.llm_budget.spend", SPEND)


def make_client(transport: FakeTransport) -> tuple[LLMClient, RecordingSpend]:
    client = LLMClient.__new__(LLMClient)  # no settings, no real SDK clients
    client.settings = SimpleNamespace(
        groq_model="test/model",
        groq_api_key="k",
        cerebras_api_key=None,
        cerebras_model=None,
        groq_daily_request_budget=900,
    )
    client.groq_client = transport
    client.cerebras_client = None
    client._budget = RequestBudget(900)
    client._groq_auth_warned = False
    client._cerebras_auth_warned = False
    return client, SPEND


class RecordingStats:
    def __init__(self):
        self.events: list[tuple[str, str, int]] = []

    def record(self, source: str, event: str, n: int = 1) -> None:
        self.events.append((source, event, n))


def attach(module, transport, stats):
    """Patch a module's get_llm_client/STATS to the fake transport + recorder."""
    client, spend = make_client(transport)
    return (
        patch.object(module, "get_llm_client", AsyncMock(return_value=client)),
        patch.object(module, "STATS", stats),
        client,
        spend,
    )


async def test_snippet_body_returns_a_real_snippet():
    """The whole point: a real call, a real snippet dict out the other end."""
    transport = FakeTransport()
    stats = RecordingStats()
    p_client, p_stats, client, spend = attach(snippet_extractor, transport, stats)

    with p_client, p_stats:
        out = await extract_snippets_from_article(
            article_id="a-1", story_id="s-1", text=ARTICLE, title="Transit bond delayed"
        )

    assert len(out) == 1
    snip = out[0]
    assert snip["text"] == (
        "Council member Dana Reyes said the shortfall was worse than reported."
    )
    assert snip["snippet_type"] == "quote"
    assert snip["entities"] == ["Dana Reyes"]
    assert snip["confidence"] == 88
    assert snip["position"] == 420000
    assert snip["article_id"] == "a-1" and snip["story_id"] == "s-1"
    # minhash is generated by production code, not faked
    assert isinstance(snip["minhash_signature"], list | set | tuple)
    assert len(snip["minhash_signature"]) > 0

    # the transport was really called, once, with the production contract
    assert len(transport.calls) == 1
    # The LITERAL, not `== SNIPPET_MAX_TOKENS`: asserting against the constant is a
    # tautology that stayed green when the constant was mutated 3000 -> 1500 (mutation
    # M4, 17 passed). The number is the thing under test -- 1500 truncated 12 of 30
    # corpus articles to nothing.
    assert SNIPPET_MAX_TOKENS == 3000
    assert transport.calls[0]["max_tokens"] == 3000
    assert transport.calls[0]["temperature"] == 0.1
    assert spend.spends == 1  # real RequestBudget reserved exactly one request

    # and the counter is the signature the function actually uses: n=, positional
    assert stats.events == [("snippets", "extracted", 1)]


async def test_snippet_body_awaits_the_client():
    """Un-awaited get_llm_client() must not be able to reach the transport.

    AsyncMock is what every other call site in the repo uses, so an un-awaited
    call hands the code a coroutine, whose .chat_completion raises
    AttributeError and the fail-soft handler returns []. This is the assertion
    that turns that silent [] into a red test.
    """
    transport = FakeTransport()
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, transport, stats)

    with p_client, p_stats:
        out = await extract_snippets_from_article(
            article_id="a-2", story_id="s-2", text=ARTICLE, title="t"
        )

    assert out, "extraction returned nothing; the client was not awaited"
    assert transport.calls, "the transport was never reached"


async def test_snippet_body_survives_a_broken_counter():
    """Bookkeeping must never be able to discard a good extraction."""
    transport = FakeTransport()
    stats = RecordingStats()

    def exploding_record(source, event, n=1):
        raise TypeError("record() got an unexpected keyword argument 'count'")

    stats.record = exploding_record
    p_client, p_stats, _, _ = attach(snippet_extractor, transport, stats)

    with p_client, p_stats:
        out = await extract_snippets_from_article(
            article_id="a-3", story_id="s-3", text=ARTICLE, title="t"
        )

    assert len(out) == 1


async def test_empty_content_is_distinguishable_from_a_provider_error(caplog):
    """A reasoning model that answers nothing is not the same event as a 429."""
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, FakeTransport(content=""), stats)

    with p_client, p_stats, caplog.at_level("WARNING"):
        out = await extract_snippets_from_article(
            article_id="a-4", story_id="s-4", text=ARTICLE, title="t"
        )

    assert out == []
    assert ("snippets", "empty_response", 1) in stats.events
    assert not any(e[1].startswith("extraction_failed") for e in stats.events)
    assert "empty content" in caplog.text


async def test_unparseable_content_is_distinguishable_too(caplog):
    """Truncated mid-string output: a parse failure, not a provider failure."""
    truncated = '[{"text": "Council member Dana Reyes said the shortfall was'
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, FakeTransport(content=truncated), stats)

    with p_client, p_stats, caplog.at_level("WARNING"):
        out = await extract_snippets_from_article(
            article_id="a-5", story_id="s-5", text=ARTICLE, title="t"
        )

    assert out == []
    assert [e for e in stats.events if e[1].startswith("parse_failed")], stats.events
    assert not any(e[1].startswith("extraction_failed") for e in stats.events)
    assert "Failed to parse snippet JSON" in caplog.text


async def test_provider_error_logs_the_real_exception_type(caplog):
    """Where the real exception is logged depends on LLMClient's own policy.

    `chat_completion` deliberately does NOT let a non-transient, non-auth provider
    error escape: it warns at llm.py:471, tries Cerebras, and only raises
    `LLMError("No LLM provider available")` if there is no fallback (llm.py:474-493).
    So the extractor sees LLMError. Asserting RuntimeError here would have been
    asserting a behaviour LLMClient is built to prevent; the point of the test is that
    the underlying cause is still visible in the log, one layer up.
    """
    transport = FakeTransport(raise_exc=RuntimeError("429 rate limit exceeded"))
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, transport, stats)

    with p_client, p_stats, caplog.at_level("WARNING"):
        out = await extract_snippets_from_article(
            article_id="a-6", story_id="s-6", text=ARTICLE, title="t"
        )

    assert out == []
    assert ("snippets", "extraction_failed:error_LLMError", 1) in stats.events
    assert "LLMError" in caplog.text
    # the real cause is preserved, at the layer that caught it
    assert "Groq chat completion failed" in caplog.text
    assert "429 rate limit exceeded" in caplog.text


async def test_exhausted_retries_surface_as_RetryError_not_the_cause(caplog):
    """Measured, and a real finding about llm.py -- do not "fix" this test to match intent.

    `_chat_completion_groq` is wrapped in @retry(stop_after_attempt(3)). When the retries
    run out tenacity raises `RetryError`, NOT the underlying httpx.ConnectError. So in
    `chat_completion` the `if self._is_transient_error(e): raise` branch is never taken
    for a retryable failure -- _is_transient_error(RetryError) is False -- and a genuine
    network outage degrades into the same `LLMError("No LLM provider available")` an
    auth failure produces. Reported as a defect in llm.py; out of scope here.
    """
    transport = FakeTransport(raise_exc=httpx.ConnectError("connection reset by peer"))
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, transport, stats)

    with p_client, p_stats, caplog.at_level("ERROR"):
        out = await extract_snippets_from_article(
            article_id="a-6b", story_id="s-6b", text=ARTICLE, title="t"
        )

    assert out == []
    assert ("snippets", "extraction_failed:error_LLMError", 1) in stats.events
    assert len(transport.calls) == 3, "tenacity really did retry three times"
    # the extractor logs the exception it was actually handed, by type and message
    assert "LLMError" in caplog.text


async def test_short_articles_short_circuit_without_a_call():
    transport = FakeTransport()
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, transport, stats)

    with p_client, p_stats:
        out = await extract_snippets_from_article(
            article_id="a-7", story_id="s-7", text="too short", title="t"
        )

    assert out == []
    assert transport.calls == []


async def test_max_snippets_caps_the_returned_list():
    payload = """```json
[
  {"text": "First snippet text that is definitely long enough to keep.", "type": "fact",
   "entities": [], "confidence": 70, "position_estimate": 0.1},
  {"text": "Second snippet text that is definitely long enough to keep.", "type": "fact",
   "entities": [], "confidence": 71, "position_estimate": 0.2},
  {"text": "Third snippet text that is definitely long enough to keep.", "type": "fact",
   "entities": [], "confidence": 72, "position_estimate": 0.3}
]
```"""
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, FakeTransport(content=payload), stats)

    with p_client, p_stats:
        out = await extract_snippets_from_article(
            article_id="a-8", story_id="s-8", text=ARTICLE, title="t", max_snippets=2
        )

    assert len(out) == 2
    assert stats.events == [("snippets", "extracted", 2)]


CLAIMS = """```json
[
  {
    "text": "The projected cost had doubled to 2.4 billion dollars",
    "type": "statistic",
    "entities": ["consulting firm"],
    "position": 120,
    "context": "a consulting firm found the projected cost had doubled"
  }
]
```"""


async def test_claim_extraction_body_returns_real_claims():
    """fact_checker.py:261 had the same un-awaited client as the snippet stage."""
    from src.reliability import fact_checker

    transport = FakeTransport(content=CLAIMS)
    p_client, _p_stats, _client, _spend = attach(fact_checker, transport, RecordingStats())

    with p_client:
        claims = await extract_claims_from_article(
            article_text=ARTICLE,
            title="Transit bond delayed",
            entities={"ORG": ["city council"]},
        )

    assert len(claims) == 1
    assert claims[0].text == "The projected cost had doubled to 2.4 billion dollars"
    assert claims[0].claim_type == "statistic"
    assert claims[0].entities == ["consulting firm"]
    assert claims[0].position == 120
    assert len(claims[0].claim_hash) == 64
    assert len(transport.calls) == 1
    assert transport.calls[0]["max_tokens"] == 2000  # literal, not the call default


async def test_claim_extraction_empty_content_is_not_a_provider_error(caplog):
    from src.reliability import fact_checker

    p_client, _p_stats, _client, _spend = attach(
        fact_checker, FakeTransport(content=""), RecordingStats()
    )

    with p_client, caplog.at_level("WARNING"):
        claims = await extract_claims_from_article(
            article_text=ARTICLE, title="t", entities={}
        )

    assert claims == []
    assert "empty content" in caplog.text
    assert "provider failure" not in caplog.text


@pytest.mark.parametrize("content", ["not json at all", '{"snippets": []}', "[1, 2, 3]"])
async def test_claim_extraction_bad_payloads_fail_soft(content, caplog):
    from src.reliability import fact_checker

    p_client, _p_stats, _client, _spend = attach(
        fact_checker, FakeTransport(content=content), RecordingStats()
    )

    with p_client, caplog.at_level("WARNING"):
        claims = await extract_claims_from_article(
            article_text=ARTICLE, title="t", entities={}
        )

    assert claims == []


# ---------------------------------------------------------------------------
# The regression d79d824 introduced: it moved the per-snippet post-processing loop
# OUT of the try block to give each failure its own event. That also removed the
# only thing standing between a malformed model field and an exception escaping the
# function. Before the refactor `int(s.get("confidence"))` raising ValueError was
# caught and became `return []`; now it propagates out of extract_snippets_from_article
# into extract_snippets_for_story, which aborts the WHOLE story's snippet list because
# it has no per-article guard. pipeline.py:127 gathers with return_exceptions=True, so
# it does not crash the run -- it just silently converts every snippet for that story
# into results["errors"]. One bad field from one article loses the rest.
# ---------------------------------------------------------------------------


async def test_malformed_snippet_fields_do_not_escape_the_function():
    """Bad confidence/position_estimate must not raise out of the extractor."""
    payload = """```json
    [
      {"text": "A perfectly good snippet of news text here.", "type": "fact",
       "entities": [], "confidence": "high", "position_estimate": 0.3},
      {"text": "Another perfectly good snippet of news text.", "type": "fact",
       "entities": [], "confidence": 70, "position_estimate": "middle"}
    ]
    ```"""
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(
        snippet_extractor, FakeTransport(content=payload), stats
    )

    with p_client, p_stats:
        out = await extract_snippets_from_article(
            article_id="a-9", story_id="s-9", text=ARTICLE, title="t"
        )

    # The right outcome is to KEEP the snippet with a defaulted field, not to drop it and
    # not to raise: the text is good, only the metadata is malformed.
    assert len(out) == 2
    assert out[0]["confidence"] == 80 and out[0]["position"] == 300000
    assert out[1]["confidence"] == 70 and out[1]["position"] == 500000
    assert stats.events[-1] == ("snippets", "extracted", 2)


async def test_one_malformed_article_does_not_take_the_story_with_it():
    """The blast radius that matters: extract_snippets_for_story loops the articles."""
    from src.enrichment.snippet_extractor import extract_snippets_for_story

    good = """```json
    [{"text": "The council delayed the transit bond on Tuesday evening.", "type": "fact",
      "entities": [], "confidence": 70, "position_estimate": 0.3}]
    ```"""
    # "bad" is a DIFFERENT snippet (so minhash dedup cannot confuse the two) carrying a
    # confidence the model invented as prose.
    bad = """```json
    [{"text": "A consulting firm found the cost had doubled to 2.4bn.", "type": "fact",
      "entities": [], "confidence": "very high", "position_estimate": 0.4}]
    ```"""

    async def extract_as(article_id, payload):
        client, _spend = make_client(FakeTransport(content=payload))
        with patch.object(snippet_extractor, "get_llm_client", AsyncMock(return_value=client)):
            return await extract_snippets_from_article(
                article_id=article_id, story_id="s-10", text=ARTICLE, title="t"
            )

    articles = [
        {"article_id": "bad", "title": "t", "body_text": ARTICLE},
        {"article_id": "good", "title": "t", "body_text": ARTICLE},
    ]
    async def pick_payload(**kw):
        return await extract_as(kw["article_id"], bad if kw["article_id"] == "bad" else good)

    # Pin the real loop, not a re-implementation of it.
    with patch.object(
        snippet_extractor, "extract_snippets_from_article", side_effect=pick_payload
    ):
        out = await extract_snippets_for_story(story_id="s-10", articles=articles)

    ids = sorted(s["article_id"] for s in out)
    assert ids == ["bad", "good"], (
        "both articles' snippets must survive: the bad one with a defaulted confidence"
    )
    by_id = {s["article_id"]: s for s in out}
    assert by_id["bad"]["confidence"] == 80


async def test_snippet_type_outside_the_enum_is_dropped_not_raised():
    """enrich_story_with_snippets does SnippetType(s['snippet_type']) unguarded."""
    from src.enrichment.snippet_extractor import extract_snippets_from_article as ex

    payload = """```json
    [{"text": "A perfectly good snippet of news text here.", "type": "haiku",
      "entities": [], "confidence": 70, "position_estimate": 0.3}]
    ```"""
    stats = RecordingStats()
    p_client, p_stats, _, _ = attach(snippet_extractor, FakeTransport(content=payload), stats)

    with p_client, p_stats:
        out = await ex(article_id="a-11", story_id="s-11", text=ARTICLE, title="t")

    valid = {"quote", "stat", "fact", "summary", "claim"}
    for s in out:
        assert s["snippet_type"] in valid, (
            f"{s['snippet_type']!r} would raise ValueError in SnippetType() and take "
            "the whole story's persistence with it"
        )
