"""Token-denominated cost accounting: the tests for src/shared/budget.py + llm_budget.py.

The claim under test is the one that failed in production: a request-denominated cap read
97% unspent on a day the free tier's 200,000-token allowance was entirely gone. So the
central tests here are not "does a token counter increment" but "does a token cap REFUSE
while the request count is far under its own cap", because a counter nobody enforces is a
number, not a cap.

Two dialects, deliberately:

* SQLite (aiosqlite) for semantics -- the cap holds across calls, a refused spend costs
  nothing, a new day is a new budget. Every budget test in this repo runs on SQLite.
* A compile check against the Postgres dialect for the counter statement, plus an
  assertion that every numeric parameter carries an explicit type at every use site.
  This is not ceremony: the statement once shipped in a form that every SQLite test
  passed and that Postgres refuses to parse ("inconsistent types deduced for parameter"),
  which the caller then read as an exhausted cap for two hours.

No database required. The live dev-database proof is the coordinator's, not a unit test.
"""

import pytest
from sqlalchemy import BigInteger, Date
from sqlalchemy.dialects import postgresql

from src.shared import budget as budget_module
from src.shared.budget import (
    COUNTERS,
    COUNTERS_BY_NAME,
    GROQ_PHASE2_TOKENS,
    GROQ_REQUEST_TOKENS,
    GROQ_REQUESTS,
    GROQ_TRANSLATION_REQUESTS,
    GROQ_TRANSLATION_TOKENS,
    MYMEMORY_CHARS,
    _SPEND,
    _USED,
    counter_cap,
    spend,
    today,
    used,
)
from src.shared.llm_budget import BudgetExhausted, RequestBudget, TokenBudget, TokenBudgetExhausted, estimate_tokens

# The counter's day is defined exactly once, in src/shared/budget.py:376 as the UTC day.
# These tests must read the counter back on the SAME day, and they must ask the product
# for it rather than re-deriving it: `date.today()` is the *local* day, so on a runner
# whose timezone is not UTC (GitHub Actions' own runners, and any developer machine not
# set to UTC) the reads below asserted against a row that nothing had ever written to.
#
# Measured, not assumed (before this change, 32 tests in this file):
#   TZ=UTC              -> 32 passed
#   TZ=America/New_York -> 5 failed, 27 passed
#   TZ=Asia/Kolkata, Pacific/Auckland -> 32 passed (their local day happens to match
#                                         UTC's at the time of the run, which is exactly
#                                         why this looks random when it is reported)
#
# The 5 failures were always these: the four TestRecordTokens cases (record() charges the
# UTC day, used(day=local) read a different row and got 0) and
# TestTokenCapRefusesWhileRequestsAreUnder::test_refuses_on_tokens_with_requests_far_under
# (the pre-spent token row landed on the UTC day while the cap under test read the local
# one, so nothing looked spent). The same defect was fixed in tests/test_llm_budget.py by
# c58c6f6; this file was missed, because it re-derived the day independently.
TODAY = today()


@pytest.fixture
def counter(budget_counter):
    """The counter this test spends against (see the autouse fixture in conftest.py)."""
    return budget_counter


# --------------------------------------------------------------------------------------
# The headline case: tokens spent, requests nowhere near their cap.
# --------------------------------------------------------------------------------------


class TestTokenCapRefusesWhileRequestsAreUnder:
    """The exact production failure, driven through TokenBudget.

    vm-main had a real incident: 28 requests against a fully spent 200,000-token
    allowance, and the request-only cap saw 28/900 = 3% and let every call through.
    HEAD splits the caps: TokenBudget.ensure_headroom refuses when the token
    allowance is gone, regardless of the request count.
    """

    async def test_refuses_on_tokens_with_requests_far_under(self, counter):
        # Protects: a spent token allowance refuses even when requests are far under.
        budget = TokenBudget(GROQ_REQUESTS, 60_000)
        await spend(GROQ_REQUEST_TOKENS, 60_000, 60_000, day=TODAY)
        await spend(GROQ_REQUESTS, 1, 900, day=TODAY)

        # The token cap is spent. ensure_headroom must refuse.
        with pytest.raises(TokenBudgetExhausted):
            await budget.ensure_headroom(100)

    async def test_the_same_budget_allows_the_call_while_tokens_remain(self, counter):
        """The control: the refusal above must come from the token cap, not from the
        harness refusing everything. Same setup, tokens left over, headroom granted."""
        # Protects: the gate is not stuck closed; headroom is granted when available.
        budget = TokenBudget(GROQ_REQUESTS, 60_000)
        await spend(GROQ_REQUEST_TOKENS, 59_000, 60_000, day=TODAY)

        # 1,000 tokens remain; a 100-token estimate fits.
        assert await budget.ensure_headroom(100) == 100

    async def test_an_oversized_prompt_is_refused_before_it_is_sent(self, counter):
        """The headroom gate uses an ESTIMATE, so a prompt that cannot fit is refused even
        though no tokens have been spent yet. This is the estimate path, not the counter
        path, and it is the only thing standing between a huge call and a blown cap."""
        # Protects: an estimate larger than the remaining allowance is refused pre-call.
        budget = TokenBudget(GROQ_REQUESTS, 1_000)

        # ~4,000 characters of prompt is ~2,000 estimated tokens against 1,000 of cap.
        with pytest.raises(TokenBudgetExhausted):
            await budget.ensure_headroom(2_000)

    async def test_empty_messages_are_not_refused_by_the_estimate(self, counter):
        """A zero-length prompt must not read as zero tokens needed and then also must not
        be treated as needing a whole call's worth. Guards the degenerate end of
        estimate_tokens, which the reasoning-model case makes reachable: an empty
        completion is a real possibility, so an empty request must still be accounted."""
        # Protects: the estimate floor (1 token) does not block empty prompts.
        budget = TokenBudget(GROQ_REQUESTS, 10)
        # A 1-token estimate fits in a 10-token cap.
        assert await budget.ensure_headroom(1) == 1


class TestRecordTokens:
    """TokenBudget.record() charges actual usage against the token counter.

    Protects: the daily token figure the operator reads must reflect what the
    provider billed, including unpriced calls and overshoot past the cap.
    """

    async def test_records_the_provider_reported_usage(self, counter):
        # Protects: billed tokens are not silently dropped from the counter.
        budget = TokenBudget(GROQ_REQUESTS, 60_000)
        assert await budget.record(2_437) == 2_437
        assert await used(GROQ_REQUEST_TOKENS, day=TODAY) == 2_437

    async def test_accumulates_across_calls(self, counter):
        # Protects: the counter is a running total, not per-call.
        budget = TokenBudget(GROQ_REQUESTS, 60_000)
        await budget.record(1_742)
        await budget.record(4_962)
        assert await used(GROQ_REQUEST_TOKENS, day=TODAY) == 6_704

    async def test_absent_usage_is_charged_one_not_zero(self, counter):
        """A provider that reports no usage must not make the counter read zero forever.

        The call happened. A counter that cannot see a call stops being a cap, and a
        reasoning model returning an empty completion is exactly the case where a
        zero-token charge would be most wrong: real tokens spent, nothing recorded.
        """
        # Protects: unpriced calls are visible as a lower bound, not invisible.
        budget = TokenBudget(GROQ_REQUESTS, 60_000)
        assert await budget.record(None) == 1
        assert await budget.record(0) == 1
        assert await used(GROQ_REQUEST_TOKENS, day=TODAY) == 2

    async def test_overshoot_is_recorded_not_discarded(self, counter):
        """The defect this batch found in its own design.

        The counter is already at its cap when a completed call reports its usage. Recording
        that through the CAPPED statement matched no row, so spend() returned None and the
        charge was dropped -- the counter stayed frozen at exactly the cap and the daily
        report showed cap/cap for a day that cost more. Money already spent must not be
        lost to the write, so record uses the unconditional record().
        """
        # Protects: the daily report shows what was actually spent, even past the cap.
        budget = TokenBudget(GROQ_REQUESTS, 10)
        await spend(GROQ_REQUEST_TOKENS, 10, 10, day=TODAY)
        assert await budget.record(5_000) == 5_000
        assert await used(GROQ_REQUEST_TOKENS, day=TODAY) == 5_010

    async def test_record_never_refuses_and_never_raises(self, counter, monkeypatch):
        # Protects: a failed write does not raise into the provider-call path.
        import src.shared.llm_budget as module

        async def unreachable(name, amount, *, day=None):
            return None  # an unreachable counter must not raise into the caller

        monkeypatch.setattr(module, "record", unreachable)
        budget = TokenBudget(GROQ_REQUESTS, 10)
        assert await budget.record(7_000) == 7_000


class TestPerConsumerSeparation:
    """Two consumers on one key that must not share a counter.

    The measured precedent is one real call through the real walk charging 3 requests to
    one rung and 1 to another, in two separate rows. Tokens must behave the same way:
    a shared token row would let the largest consumer hide the others' exhaustion, and
    Phase 2 alone costs ~2,437 tokens per story.
    """

    async def test_token_counters_are_separate_rows(self, counter):
        await spend(GROQ_REQUEST_TOKENS, 60_000, 60_000, day=TODAY)
        assert await used(GROQ_TRANSLATION_TOKENS, day=TODAY) == 0
        assert await used(GROQ_PHASE2_TOKENS, day=TODAY) == 0

    async def test_exhausting_one_token_counter_leaves_the_others_spendable(self, counter):
        await spend(GROQ_REQUEST_TOKENS, 60_000, 60_000, day=TODAY)
        assert await spend(GROQ_TRANSLATION_TOKENS, 20_000, 20_000, day=TODAY) == 20_000
        assert await spend(GROQ_PHASE2_TOKENS, 40_000, 40_000, day=TODAY) == 40_000

    async def test_every_existing_request_counter_still_behaves_as_before(self, counter):
        """Preservation, not just addition. These three names are spent against by other
        stages, so their behaviour is a contract this batch must not break."""
        assert await spend(GROQ_REQUESTS, 1, 900, day=TODAY) == 1
        assert await spend(GROQ_REQUESTS, 1, 900, day=TODAY) == 2
        assert await used(GROQ_REQUESTS, day=TODAY) == 2

        assert await spend(GROQ_TRANSLATION_REQUESTS, 1, 300, day=TODAY) == 1
        assert await used(GROQ_TRANSLATION_REQUESTS, day=TODAY) == 1

        assert await spend(MYMEMORY_CHARS, 100, 500, day=TODAY) == 100
        assert await spend(MYMEMORY_CHARS, 500, 500, day=TODAY) is None
        assert await used(MYMEMORY_CHARS, day=TODAY) == 100

        assert await spend(GROQ_PHASE2_TOKENS, 2_437, 40_000, day=TODAY) == 2_437
        assert await used(GROQ_PHASE2_TOKENS, day=TODAY) == 2_437

    async def test_token_spend_does_not_disturb_the_request_counter(self, counter):
        """The two caps share a stage, not a row. A token charge must not move the
        request counter, or the request cap would start refusing on token volume."""
        await spend(GROQ_REQUESTS, 1, 900, day=TODAY)
        await spend(GROQ_REQUEST_TOKENS, 59_999, 60_000, day=TODAY)
        assert await used(GROQ_REQUESTS, day=TODAY) == 1


class TestUnreadableCountersRefuse:
    async def test_an_unreadable_request_counter_refuses(self, counter, monkeypatch):
        """The request cap is enforced by the RESERVATION (a write), not by a read, so an
        unreachable counter refuses through spend() returning None. Patching `used` here
        would prove nothing: that function is only consulted once something else has
        already refused. This was the wrong mechanism in the first version of this test.
        """
        import src.shared.llm_budget as module

        async def unreachable(name, amount, cap, *, day=None):
            return None  # exactly what spend() returns for an unreachable counter

        monkeypatch.setattr(module, "spend", unreachable)
        budget = RequestBudget(daily_limit=900)
        sent = []

        async def call():
            sent.append(1)
            return {}

        with pytest.raises(BudgetExhausted):
            await budget.run(call, [{"role": "user", "content": "hi"}], "m")
        assert sent == []

    async def test_an_unreadable_token_counter_refuses(self, counter, monkeypatch):
        """An unreadable TOKEN counter must refuse even when the request counter is
        comfortably unspent. Refusing only on the request counter would let a run spend
        a whole day of tokens against a cap it could not read."""
        # Protects: an unverifiable token budget is not an unlimited one.
        budget = TokenBudget(GROQ_REQUESTS, 60_000)
        await spend(GROQ_REQUESTS, 5, 900, day=TODAY)

        async def unreadable(name, *, day=None):
            return 5 if name == GROQ_REQUESTS else None

        monkeypatch.setattr("src.shared.llm_budget.used", unreadable)
        # spent_today returns -1 for unreadable; remaining() maps that to 0,
        # so any positive estimate is refused.
        assert await budget.spent_today() == -1
        with pytest.raises(TokenBudgetExhausted):
            await budget.ensure_headroom(100)


# --------------------------------------------------------------------------------------
# The failure direction of spend() itself.
# --------------------------------------------------------------------------------------


class TestSpendFailureMapping:
    """Enumerate every failure spend() can map onto a fail-safe value.

    None means "cap spent" to every caller, so the set of exceptions that become None is a
    safety property, not an implementation detail. Operational failures must be in it; a
    statement the server REJECTS must not be, because that is a defect and reading it as
    "spent" is what turned a bug into two hours of refused calls with nothing in the logs
    but a warning.
    """

    async def test_operational_failure_still_fails_safe(self, monkeypatch):
        """Unreachable database / missing migration: refuse, do not fall open."""
        engine = budget_module._get_engine.__wrapped__ if False else None  # noqa: F841
        import sqlalchemy

        class Unreachable:
            def connect(self):
                raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("down"))

        monkeypatch.setattr(budget_module, "_get_engine", lambda: Unreachable())
        assert await spend(GROQ_REQUEST_TOKENS, 1, 100, day=TODAY) is None

    async def test_programming_error_is_raised_not_mapped_to_spent(self, monkeypatch):
        """The regression test for the two-hour outage, at the level it happened.

        A ProgrammingError means the server parsed our SQL and rejected it. Returning None
        for it presents a defect as a healthy exhausted budget at every call site.
        """
        import sqlalchemy

        class Rejects:
            def connect(self):
                raise sqlalchemy.exc.ProgrammingError(
                    "INSERT ...", {}, Exception("inconsistent types deduced for parameter $3")
                )

        monkeypatch.setattr(budget_module, "_get_engine", lambda: Rejects())
        with pytest.raises(sqlalchemy.exc.ProgrammingError):
            await spend(GROQ_REQUEST_TOKENS, 1, 100, day=TODAY)


# --------------------------------------------------------------------------------------
# The production dialect.
# --------------------------------------------------------------------------------------


class TestPostgresDialect:
    """Compile the counter statements the way production compiles them.

    SQLite accepts bound values without ever asking Postgres-style type inference a
    question, so a green SQLite suite is silent about this entire class of failure. These
    tests are cheap and they are the ones that would have caught it.
    """

    @pytest.mark.parametrize("statement", [_SPEND, _USED], ids=["spend", "used"])
    def test_compiles_on_the_postgres_dialect(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        assert str(compiled).strip(), "statement compiled to nothing"
        # Every parameter the SQL text mentions must survive into the compiled binds,
        # or the driver is being handed a statement with an unfillable hole.
        import re

        mentioned = set(re.findall(r":(\w+)", str(statement)))
        assert mentioned, "no named parameters found -- the test is not testing anything"
        assert mentioned <= set(compiled.binds), (
            f"parameters {sorted(mentioned - set(compiled.binds))} are used in the SQL "
            "but carry no compiled bind"
        )

    def test_every_numeric_parameter_carries_an_explicit_type(self):
        """`:amount` is used twice in _SPEND -- in the VALUES list, where Postgres types
        it bigint from the target column, and inside the comparison, where both operands
        are untyped and resolve integer. That mismatch is the exact
        "inconsistent types deduced for parameter $3" error. Naming the type removes the
        inference, so this asserts the names are present rather than trusting it."""
        # `_SPEND.bindparams` is the CONSTRUCTOR method, not the mapping -- reading it
        # as a dict is a TypeError that looks like a SQLAlchemy version problem. The
        # compiled statement is the public view of the same information.
        types = {name: b.type for name, b in _SPEND.compile(dialect=postgresql.dialect()).binds.items()}
        assert types["amount"].__class__ is BigInteger
        assert types["cap"].__class__ is BigInteger
        assert types["day"].__class__ is Date

    def test_spend_binds_carry_bigint_casts_for_asyncpg(self):
        """Every use of :amount and :cap must render with ::BIGINT under asyncpg.

        The untyped SELECT form failed on Postgres with "inconsistent types deduced".
        Typed binds (BigInteger) fix it. This test compiles with the asyncpg dialect
        and requires the cast on every parameter use, so a regression to untyped
        binds is caught before it reaches Postgres.
        """
        import re
        from sqlalchemy.dialects.postgresql import asyncpg
        compiled = _SPEND.compile(dialect=asyncpg.dialect())
        sql = str(compiled)
        # Find the positional numbers for amount and cap from the compiled
        # statement, so the test does not hardcode parameter order.
        positions = {name: i + 1 for i, name in enumerate(compiled.positiontup)}
        assert "amount" in positions, f"amount not in statement: {sql}"
        assert "cap" in positions, f"cap not in statement: {sql}"
        amount_pos = positions["amount"]
        cap_pos = positions["cap"]
        # Vacuity guard: the casts must actually appear, so a rewrite that
        # drops the parameters cannot pass vacuously.
        assert f"${amount_pos}::BIGINT" in sql, f"amount cast missing: {sql}"
        assert f"${cap_pos}::BIGINT" in sql, f"cap cast missing: {sql}"
        # No use of amount or cap may appear without a ::BIGINT cast.
        # A bare parameter means a typed bind was lost, and Postgres will fail
        # with "inconsistent types deduced".
        assert not re.search(rf"\${amount_pos}(?!::BIGINT)", sql), (
            f"amount missing BIGINT cast: {sql}"
        )
        assert not re.search(rf"\${cap_pos}(?!::BIGINT)", sql), (
            f"cap missing BIGINT cast: {sql}"
        )

    def test_used_statement_types_its_day_parameter(self):
        day_bind = _USED.compile(dialect=postgresql.dialect()).binds["day"]
        assert day_bind.type.__class__ is Date


# --------------------------------------------------------------------------------------
# The registry the daily report reads.
# --------------------------------------------------------------------------------------


class TestCounterRegistry:
    def test_every_counter_declares_its_unit(self):
        for spec in COUNTERS:
            assert spec.unit in {"requests", "tokens", "chars"}, (
                f"{spec.name} declares unit {spec.unit!r}, which the report cannot render"
            )

    def test_every_request_counter_declares_a_token_partner(self):
        """The defect this batch fixes, as an invariant: every request-denominated
        counter names its token-denominated partner. A new Groq spend() call site added
        without one is exactly how caption/classification became unreadable in tokens.

        Checked against the declared `pairs_with`, not against a name-derived guess: the
        pair is `groq_requests` <-> `groq_request_tokens`, which no string rule produces.
        """
        for spec in COUNTERS:
            if spec.unit != "requests":
                continue
            partner = COUNTERS_BY_NAME.get(spec.pairs_with or "")
            assert partner is not None, f"{spec.name} names no existing token partner"
            assert partner.unit == "tokens", (
                f"{spec.name} pairs with {partner.name}, which counts {partner.unit}"
            )

    def test_a_token_counter_may_have_no_request_partner(self):
        """The invariant is one-directional and deliberately so.

        Every REQUEST counter needs a token partner -- that is the hole this batch closes.
        A token counter does not need a request partner: groq_phase2_tokens has never had
        one, because Phase 2 was denominated in tokens from the start. Asserting symmetry
        would force a fake request counter into existence purely to satisfy the test.
        """
        unpaired = [s.name for s in COUNTERS if s.unit == "tokens" and not s.pairs_with]
        assert unpaired == [GROQ_PHASE2_TOKENS], (
            f"unexpected token counters with no request partner: {unpaired}"
        )
        for spec in COUNTERS:
            if spec.unit == "tokens" and spec.pairs_with:
                partner = COUNTERS_BY_NAME[spec.pairs_with]
                assert partner.unit == "requests"

    def test_pairing_is_symmetric(self):
        """An asymmetric pairing would let the report compare a token counter against a
        request counter for a DIFFERENT stage, which reads as a real finding and is not."""
        for spec in COUNTERS:
            if not spec.pairs_with:
                continue
            partner = COUNTERS_BY_NAME[spec.pairs_with]
            assert partner.pairs_with == spec.name, (
                f"{spec.name} pairs with {partner.name}, which pairs back with "
                f"{partner.pairs_with!r}"
            )

    def test_registry_covers_every_counter_named_in_the_module(self):
        """A counter that is spendable but not in the registry is a counter whose daily
        cost is invisible. This walks the module's own names rather than trusting that
        whoever added a counter remembered to register it."""
        import src.shared.budget as mod

        named = {
            value
            for name, value in vars(mod).items()
            if name.isupper() and isinstance(value, str) and value.endswith(
                ("requests", "_tokens", "_chars")
            )
        }
        assert named <= set(COUNTERS_BY_NAME), (
            f"counters not in the registry: {sorted(named - set(COUNTERS_BY_NAME))}"
        )

    def test_caps_come_from_settings_not_hardcoded_in_the_report(self):
        # The report resolves each cap through counter_cap, so a settings change moves the
        # report. Assert the wiring rather than the number, which is a default not a fact.
        assert counter_cap(COUNTERS_BY_NAME[GROQ_REQUEST_TOKENS]) > 0
        assert counter_cap(COUNTERS_BY_NAME[GROQ_REQUESTS]) > 0


def test_estimate_matches_the_phase2_arithmetic():
    """llm_budget.estimate_tokens duplicates phase2.estimate_prompt_tokens rather than
    importing it (importing the verification package into the shared client would drag its
    schema imports into every LLM caller). Duplication rots, so it is pinned here."""
    from src.verification.phase2 import estimate_prompt_tokens

    for prompt in ("", "a", "hello world", "x" * 4_000, "unicode: éèê " * 50):
        as_message = estimate_tokens([{"role": "user", "content": prompt}])
        assert as_message == estimate_prompt_tokens(prompt), (
            f"the two estimates disagree on a {len(prompt)}-character prompt"
        )


def test_estimate_ignores_non_content_message_keys():
    """A message carrying a role but no content must not raise, and must not be read as
    free: the +1 keeps it at one token."""
    assert estimate_tokens([{"role": "user"}]) == 1
    assert estimate_tokens([{"role": "user", "content": None}]) == 1


def test_token_budget_limit_is_explicit_not_from_settings():
    """TokenBudget takes its limit as a constructor argument.

    Protects: the token cap is visible at the construction site, not resolved
    by magic from settings. The settings-to-budget wiring lives in
    LLMClient._token_budget_for, which is covered by the roster consistency test.
    """
    budget = TokenBudget(GROQ_REQUESTS, 60_000)
    assert budget.daily_limit == 60_000
    assert budget.daily_limit > 0


def test_the_three_groq_token_caps_fit_inside_the_published_allowance():
    """Not a test of behaviour -- a test of the arithmetic in the comments.

    Groq's free plan publishes 200,000 tokens/day for the whole key, shared by all three
    Groq consumers. Three independently sized caps that each look generous is how three
    stages each come to believe they own the tier, so the sum is asserted against the real
    allowance with headroom left over.
    """
    # Protects: the Groq stages cannot collectively exceed Groq's published allowance.
    groq_allowance = 200_000
    total = sum(
        counter_cap(spec) for spec in COUNTERS
        if spec.unit == "tokens" and "groq" in spec.name
    )
    assert total <= groq_allowance, (
        f"the Groq token caps sum to {total}, over the published {groq_allowance}/day"
    )
    assert total <= groq_allowance * 0.75, "no headroom left for an unmeasured consumer"