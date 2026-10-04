"""Token-denominated accounting for the LLM roster: who paid, in whose unit, and when it stops.

The request counters this suite sits next to were sufficient while a call cost roughly one
request. They stopped being sufficient on 2026-10-03, when 28 Groq requests against a
1,000/day request cap were joined by a full 200,000-token day -- the cap that binds is the
provider's token limit, and a request counter cannot see it. Everything here exists to keep
three claims true at once, which is the part that is easy to get wrong:

1. **Isolation, both directions.** A rung's tokens land on the rung's own row, and one
   rung's tokens cannot be spent to pay for another rung's call. Sharing one row would let
   Groq's spend report itself as Cerebras's allowance.
2. **A missing measurement is unknown, never zero.** A zero asserts the call was free.
   Zero is what makes a cap decorative, so an unpriced call is charged a one-token floor
   and counted separately.
3. **The gate and the record are different jobs.** The gate refuses a call that cannot fit;
   the record charges what the call actually cost. Refusing to record a call that already
   happened would understate the day and make the *next* gate think there is room left.

No network: every transport is a stub, and the counters are the real SQLite
`budget_counters` table from conftest's `budget_counter` fixture, so the cap arithmetic
under test is the production arithmetic and only the provider is fake.
"""

from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared import llm_budget
from src.shared.budget import (
    CEREBRAS_REQUESTS,
    CEREBRAS_TOKENS,
    GROQ_REQUESTS,
    GROQ_TOKENS,
    OPENROUTER_GEMMA_TOKENS,
    spend,
    today,
    unpriced_calls_counter_name,
    used,
)
from src.shared.llm import LLMClient, LLMError
from src.shared.llm_budget import (
    BudgetExhausted,
    TokenBudget,
    TokenBudgetExhausted,
    _usage_int,
    extract_total_tokens,
    upper_bound_tokens,
)
from src.shared.llm_roster import ROSTER, daily_roster_cost

MESSAGES = [{"role": "user", "content": "test"}]


def _completion(content: str, *, total_tokens=None, prompt_tokens=None,
                completion_tokens=None, usage=True):
    """A Groq-shaped completion. `usage=False` is a provider that reported nothing at all."""
    message = MagicMock(content=content)
    if usage:
        response_usage = MagicMock()
        for field, value in (("total_tokens", total_tokens),
                             ("prompt_tokens", prompt_tokens),
                             ("completion_tokens", completion_tokens)):
            if value is None:
                delattr(response_usage, field)
            else:
                setattr(response_usage, field, value)
    else:
        response_usage = None
    return MagicMock(
        choices=[MagicMock(message=message, finish_reason="stop")],
        usage=response_usage,
    )


def _client(groq=None, cerebras=None, openrouter_key=None):
    """An LLMClient with exactly the rungs the test names, and nothing else."""
    client = LLMClient()
    client.groq_client = groq
    client.cerebras_client = cerebras
    client._openrouter_key = openrouter_key
    return client


def _priced(groq, content="from groq", **usage):
    groq.chat.completions.create.return_value = _completion(content, **usage)
    return groq


async def _counter(name: str) -> int:
    return await used(name, day=today())


# ---------------------------------------------------------------- isolation


class TestTokensAreChargedToTheRungThatSpentThem:
    @pytest.mark.asyncio
    async def test_a_groq_call_charges_groq_and_nobody_else(self):
        groq, cerebras = _priced(AsyncMock(), total_tokens=1234), AsyncMock()

        await _client(groq, cerebras).chat_completion(MESSAGES)

        assert await _counter(GROQ_TOKENS) == 1234
        assert await _counter(CEREBRAS_TOKENS) == 0
        assert await _counter(OPENROUTER_GEMMA_TOKENS) == 0

    @pytest.mark.asyncio
    async def test_a_cerebras_call_charges_cerebras_and_nobody_else(self):
        # No Groq at all, so the walk actually lands on Cerebras rather than stopping
        # at the first rung: a test that priced Groq would pass while Cerebras never ran.
        cerebras = AsyncMock()
        cerebras.chat.completions.create.return_value = _completion(
            "from cerebras", total_tokens=77,
        )

        await _client(None, cerebras).chat_completion(MESSAGES)

        assert await _counter(CEREBRAS_TOKENS) == 77
        assert await _counter(GROQ_TOKENS) == 0
        # Cerebras previously spent against no counter at all; its traffic being
        # uncountable was one of the three defects this accounting exists to close.
        assert await _counter(CEREBRAS_REQUESTS) == 1

    @pytest.mark.asyncio
    async def test_two_calls_sum_on_one_row(self):
        groq = _priced(AsyncMock(), total_tokens=10)
        client = _client(groq, None)

        await client.chat_completion(MESSAGES)
        await client.chat_completion(MESSAGES)

        assert await _counter(GROQ_TOKENS) == 20

    @pytest.mark.asyncio
    async def test_a_roster_call_does_not_touch_phase2s_separate_token_row(self):
        """Phase 2 drives its own budget (src/verification/phase2.py never imports
        LLMClient), so if the two shared a row either could starve the other. The
        roster's tokens must not appear on the row Phase 2 gates itself with."""
        groq = _priced(AsyncMock(), total_tokens=4321)

        await _client(groq, None).chat_completion(MESSAGES)

        assert await _counter("groq_phase2_tokens") == 0
        assert await _counter(GROQ_TOKENS) == 4321


# ---------------------------------------------------------------- the gate


class TestTheCapRefusesAndCannotBeBoughtOff:
    @pytest.mark.asyncio
    async def test_a_spent_cap_stops_the_call_before_it_is_made(self):
        groq, cerebras = _priced(AsyncMock(), total_tokens=10), AsyncMock()
        client = _client(groq, cerebras)
        client.settings.groq_daily_token_cap = 5

        await client.chat_completion(MESSAGES)

        # The whole point of a gate: the call is never sent, so the provider's day is
        # never spent. Asserted on the transport, not on the counter, because a cap
        # enforced after the fact would leave the request row incremented too.
        groq.chat.completions.create.assert_not_called()
        assert await _counter(GROQ_TOKENS) == 0
        assert await _counter(GROQ_REQUESTS) == 0
        assert "groq" in client._demoted

    @pytest.mark.asyncio
    async def test_one_rungs_tokens_cannot_be_spent_on_anothers_call(self):
        """The isolation that matters in production: a full Groq day must not be able to
        answer for Cerebras, and Cerebras having tokens left must not make a call Groq
        cannot afford look affordable."""
        groq, cerebras = _priced(AsyncMock(), total_tokens=999), AsyncMock()
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)
        client.settings.groq_daily_token_cap = 10
        client.settings.cerebras_daily_token_cap = 1_000_000

        result = await client.chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "from cerebras"
        assert await _counter(GROQ_TOKENS) == 0
        assert await _counter(CEREBRAS_REQUESTS) == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_counter_is_treated_as_spent_for_that_rung_only(self, monkeypatch):
        """A budget that cannot be verified must not become an unlimited one -- and it
        must not become a global one either, or a transient read failure on Groq would
        take Cerebras down with it."""
        groq, cerebras = _priced(AsyncMock(), total_tokens=5), AsyncMock()
        cerebras.chat.completions.create.return_value = _completion("from cerebras")
        client = _client(groq, cerebras)

        real_used = llm_budget.used

        async def unreadable_groq(name, *, day=None):
            return None if name == GROQ_TOKENS else await real_used(name, day=day)

        monkeypatch.setattr(llm_budget, "used", unreadable_groq)

        result = await client.chat_completion(MESSAGES)

        groq.chat.completions.create.assert_not_called()
        assert result["choices"][0]["message"]["content"] == "from cerebras"

    @pytest.mark.asyncio
    async def test_a_rung_without_a_token_cap_spends_nothing(self):
        """`daily_token_cap_attr` defaults to 0, which refuses everything. The direction
        matters: a new rung added to ROSTER without a cap must cost nothing rather than
        cost whatever the provider allows."""
        client = _client(_priced(AsyncMock(), total_tokens=10), None)
        rung = next(r for r in ROSTER if r.name == "groq")
        stripped = type(rung)(**{**rung.__dict__, "daily_token_cap_attr": ""})

        assert client._token_budget_for(stripped).daily_limit == 0
        with pytest.raises(TokenBudgetExhausted):
            await client._token_budget_for(stripped).ensure_headroom(1)

    @pytest.mark.asyncio
    async def test_the_refusal_is_a_budget_exhaustion_so_the_walk_demotes(self):
        """Not a generic error: a new exception type that is not a BudgetExhausted gets
        caught as "provider failed", which is indistinguishable from a 429 and would make
        a capped rung look broken instead of capped."""
        assert issubclass(TokenBudgetExhausted, BudgetExhausted)

    @pytest.mark.asyncio
    async def test_the_walk_records_the_two_refusals_under_different_keys(self, monkeypatch):
        """'No requests left' and 'no tokens left' look identical in a stat and mean
        opposite things: the first says the day's call count ran out, the second says the
        day's allowance ran out, which is the one that actually binds (28 requests against
        a spent 200,000 tokens, 2026-10-03)."""
        from src.utils.ingest_stats import STATS
        monkeypatch.setattr(STATS, "_counts", Counter(), raising=False)

        client = _client(_priced(AsyncMock(), total_tokens=10), None)
        client.settings.groq_daily_request_budget = 0
        client.settings.groq_daily_token_cap = 10 ** 9
        with pytest.raises(LLMError):
            await client.chat_completion(MESSAGES)
        assert STATS.snapshot().get("groq.budget_skipped") == 1
        assert "groq.token_budget_skipped" not in STATS.snapshot()

        monkeypatch.setattr(STATS, "_counts", Counter(), raising=False)
        client = _client(_priced(AsyncMock(), total_tokens=10), None)
        client.settings.groq_daily_request_budget = 900
        client.settings.groq_daily_token_cap = 1
        with pytest.raises(LLMError):
            await client.chat_completion(MESSAGES)
        assert STATS.snapshot().get("groq.token_budget_skipped") == 1
        assert "groq.budget_skipped" not in STATS.snapshot()

    def test_the_estimate_is_an_upper_bound_not_a_guess(self):
        """max_tokens is the provider's own ceiling on the completion, and the prompt
        side is estimated at characters/2 against English's ~4 chars/token. Both terms
        over-estimate, so the number is a ceiling: the cap can only be crossed by a call
        that cost more than its own ceiling."""
        bound = upper_bound_tokens([{"role": "user", "content": "x" * 400}], 500)
        assert bound >= 500 + 1
        # A prompt whose repr raises is not a crash: an unprintable prompt is still a
        # prompt, and it is still gated -- at the prompt-less floor, never at zero.
        class Hostile:
            def __repr__(self):
                raise ValueError("no repr for you")

        assert upper_bound_tokens(Hostile(), 500) == 1 + 500
        assert upper_bound_tokens([{"role": "user", "content": "x" * 400}], -5) >= 201


# ---------------------------------------------------------------- unknown usage


class TestAMissingMeasurementIsUnknownNotZero:
    @pytest.mark.asyncio
    async def test_a_completion_with_no_usage_still_returns_its_content(self):
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion("still answered", usage=False)

        result = await _client(groq, None).chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "still answered"

    @pytest.mark.asyncio
    async def test_no_usage_records_a_floor_and_an_unpriced_call_not_a_zero(self):
        """Zero is the one value that cannot be allowed through: it asserts the call was
        free, and a cap reading it stops capping."""
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion("ok", usage=False)

        await _client(groq, None).chat_completion(MESSAGES)

        assert await _counter(GROQ_TOKENS) == 1
        assert await _counter(unpriced_calls_counter_name(GROQ_TOKENS)) == 1
        assert await _counter(GROQ_REQUESTS) == 1

    @pytest.mark.asyncio
    async def test_a_recorded_call_is_never_dropped_because_the_counter_was_full(self):
        """The gate stops the NEXT call; a call that already happened and was already
        billed must still be recorded, or the day understates itself and the next gate
        sees allowance that is gone."""
        groq = _priced(AsyncMock(), total_tokens=5_000)
        client = _client(groq, None)
        client.settings.groq_daily_token_cap = 1
        budget = client._token_budget_for(LLMClient._rung("groq"))

        # Direct, so the pre-call gate is not in the way -- this is about record().
        assert await budget.record(5_000) == 5_000
        assert await _counter(GROQ_TOKENS) == 5_000
        with pytest.raises(TokenBudgetExhausted):
            await budget.ensure_headroom(1)

    @pytest.mark.asyncio
    async def test_accounting_failure_does_not_take_down_an_answered_call(self, monkeypatch):
        """A lost charge is bad. Refusing to return an answer the caller already paid for
        is worse -- and the LLM call is the expensive non-repeatable step.

        Only the token seam is broken. The request reservation goes through the same
        `spend()` and is deliberately left working, so this is the post-call accounting
        failing and not the pre-call budget refusing."""
        groq = _priced(AsyncMock(), total_tokens=1234)
        client = _client(groq, None)
        real_spend = llm_budget.spend

        async def broken_for_tokens(name, amount, cap, **kw):
            if name == GROQ_TOKENS:
                raise RuntimeError("counter table is on fire")
            return await real_spend(name, amount, cap, **kw)

        monkeypatch.setattr(llm_budget, "spend", broken_for_tokens)

        result = await client.chat_completion(MESSAGES)

        assert result["choices"][0]["message"]["content"] == "from groq"
        groq.chat.completions.create.assert_called_once()
        assert await _counter(GROQ_REQUESTS) == 1

    def test_a_malformed_count_is_unknown_rather_than_a_number(self):
        """`isinstance(True, int)` is True, so a bool would become 1; and `used` is a
        plain BIGINT with no CHECK, so a negative write would be accepted by the database
        and corrupt every later read."""
        for bad in (True, False, None, -1, -0.5, 1.5, "", "  ", "12a", [], {}, object()):
            assert _usage_int(bad) is None, bad
        assert _usage_int(0) == 0
        assert _usage_int(7) == 7
        assert _usage_int(7.0) == 7
        assert _usage_int(" 4242 ") == 4242


# ---------------------------------------------------------------- itemised usage


class TestItemisedUsageIsNeitherNegativeNorDoubleCounted:
    def test_total_wins_and_the_components_are_not_added_to_it(self):
        """The one shape that would produce a number twice the bill: a provider that
        reports total_tokens alongside prompt and completion is not reporting twice."""
        assert extract_total_tokens({"usage": {
            "total_tokens": 100, "prompt_tokens": 70, "completion_tokens": 30,
        }}) == 100

    def test_components_are_summed_when_the_provider_omits_the_total(self):
        assert extract_total_tokens({"usage": {
            "prompt_tokens": 70, "completion_tokens": 30,
        }}) == 100

    def test_request_without_completion_does_not_go_negative(self):
        """A response truncated mid-stream itemises the prompt and nothing else. The
        completion is absent, not zero and not -1, and the sum is what was reported."""
        assert extract_total_tokens({"usage": {"prompt_tokens": 70}}) == 70

    def test_an_explicit_zero_completion_is_still_zero(self):
        assert extract_total_tokens({"usage": {
            "prompt_tokens": 70, "completion_tokens": 0,
        }}) == 70

    @pytest.mark.parametrize("result", [
        {},                                              # no usage key at all
        {"usage": None},                                 # provider reported nothing
        {"usage": "4100"},                               # not an object
        {"usage": {}},                                   # nothing usable
        {"usage": {"total_tokens": None}},
        {"usage": {"total_tokens": -5}},
        "not a dict",
        None,
    ])
    def test_nothing_to_report_means_unknown(self, result):
        assert extract_total_tokens(result) is None

    @pytest.mark.asyncio
    async def test_an_itemised_walk_charges_the_sum_exactly_once(self):
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion(
            "ok", prompt_tokens=70, completion_tokens=30,
        )

        await _client(groq, None).chat_completion(MESSAGES)

        assert await _counter(GROQ_TOKENS) == 100
        assert await _counter(unpriced_calls_counter_name(GROQ_TOKENS)) == 0

    @pytest.mark.asyncio
    async def test_a_provider_reporting_strings_is_priced_not_declared_unknown(self):
        """OpenRouter has been observed to send counts that way."""
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion("ok", total_tokens="1234")

        await _client(groq, None).chat_completion(MESSAGES)

        assert await _counter(GROQ_TOKENS) == 1234
        assert await _counter(unpriced_calls_counter_name(GROQ_TOKENS)) == 0


# ---------------------------------------------------------------- the daily question


class TestWhatDidTheRosterCostToday:
    @pytest.mark.asyncio
    async def test_the_answer_is_per_rung_in_the_providers_own_unit(self, budget_counter,
                                                                   monkeypatch):
        from src.shared import llm_roster
        monkeypatch.setattr(llm_roster, "_get_engine", lambda: budget_counter)
        groq = _priced(AsyncMock(), total_tokens=1234)
        await _client(groq, None).chat_completion(MESSAGES)
        # Phase 2's row, which belongs to no rung: named, not silently dropped, so
        # "the roster cost nothing" can never be mistaken for "nothing else spent".
        await spend("groq_phase2_tokens", 999, 10 ** 9)

        cost = await daily_roster_cost(settings=_settings())

        by_name = {r.rung: r for r in cost.rungs}
        assert by_name["groq"].tokens == 1234
        assert by_name["groq"].requests == 1
        assert by_name["groq"].token_cap == 120_000
        assert by_name["cerebras"].tokens == 0          # a real 0: nothing spent
        assert cost.unknown_counters == ("groq_phase2_tokens",)
        assert "LOWER BOUND" not in cost.render()
        assert "groq_phase2_tokens" in cost.render()

    @pytest.mark.asyncio
    async def test_a_lower_bound_announces_itself_in_the_render(self, budget_counter,
                                                               monkeypatch):
        from src.shared import llm_roster
        monkeypatch.setattr(llm_roster, "_get_engine", lambda: budget_counter)
        groq = AsyncMock()
        groq.chat.completions.create.return_value = _completion("ok", usage=False)

        await _client(groq, None).chat_completion(MESSAGES)
        cost = await daily_roster_cost(settings=_settings())

        by_name = {r.rung: r for r in cost.rungs}
        assert by_name["groq"].unpriced_calls == 1
        assert by_name["groq"].spend_is_lower_bound is True
        assert "LOWER BOUND" in cost.render()

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_is_never_a_confident_zero(self, monkeypatch):
        from src.shared import llm_roster
        monkeypatch.setattr(llm_roster, "_get_engine", lambda: None)

        cost = await daily_roster_cost(settings=_settings())

        # 0 would answer "this rung spent nothing", which is a claim about a provider
        # this function never reached. None answers "we do not know".
        assert all(r.tokens is None for r in cost.rungs)
        assert "unreadable" in cost.render()

    @pytest.mark.asyncio
    async def test_it_answers_for_a_day_with_nothing_on_it(self, budget_counter, monkeypatch):
        from datetime import date
        from src.shared import llm_roster
        monkeypatch.setattr(llm_roster, "_get_engine", lambda: budget_counter)
        await _client(_priced(AsyncMock(), total_tokens=1234), None).chat_completion(MESSAGES)

        cost = await daily_roster_cost(day=date(2020, 1, 1), settings=_settings())

        assert cost.day == "2020-01-01"
        assert all(r.tokens == 0 for r in cost.rungs)

    @pytest.mark.asyncio
    async def test_the_token_rows_are_derived_from_the_request_rows(self):
        """Derived, so a rung cannot acquire two names for the same spend, and two rungs
        cannot collide on one row."""
        names = [llm_budget.token_counter_name(r.budget_name) for r in ROSTER]
        assert names == [GROQ_TOKENS, "openrouter_gemma_request_tokens",
                         "openrouter_nemotron_request_tokens", CEREBRAS_TOKENS]
        assert len(set(names)) == len(names)
        assert unpriced_calls_counter_name(GROQ_TOKENS) == "groq_request_unpriced_calls"

        budget = TokenBudget(GROQ_REQUESTS, 1000)
        assert (budget.name, budget.request_counter, budget.unpriced_name) == (
            GROQ_TOKENS, GROQ_REQUESTS, "groq_request_unpriced_calls",
        )


class TestTheOperatorCanAskWithoutWritingPython:
    """The brief asked for "a way to ask", and a function nobody can reach from a shell
    is a library, not a way. The script is a wrapper and nothing more: if it ever edits
    what it reports, the report stops being evidence."""

    @pytest.mark.asyncio
    async def test_the_script_prints_the_day_and_exits_two_when_unreadable(self, monkeypatch,
                                                                          capsys):
        from scripts import report_daily_cost as cli
        from src.shared import llm_roster
        monkeypatch.setattr(llm_roster, "_get_engine", lambda: None)

        assert await cli._main([]) == 2
        out = capsys.readouterr().out
        assert "unreadable" in out
        # The two failure modes must not share an exit code: one is a quiet day, the
        # other is an answer nobody has.
        assert await cli._main(["2020-01-01"]) == 2

    @pytest.mark.asyncio
    async def test_the_script_emits_machine_readable_numbers(self, budget_counter,
                                                             monkeypatch, capsys):
        import json

        from scripts import report_daily_cost as cli
        from src.shared import llm_roster
        monkeypatch.setattr(llm_roster, "_get_engine", lambda: budget_counter)
        await _client(_priced(AsyncMock(), total_tokens=1234), None).chat_completion(MESSAGES)

        assert await cli._main(["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        by_name = {r["rung"]: r for r in payload["rungs"]}
        assert by_name["groq"]["tokens"] == 1234
        assert by_name["groq"]["token_cap"] == 120_000
        assert payload["unreadable"] is False
        assert payload["counters_not_in_the_roster"] == []


def _settings():
    from src.shared.config import get_settings
    return get_settings()