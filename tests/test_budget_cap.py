"""The cap has to hold on the day's FIRST spend, not just the second one.

`spend()` is one statement doing an upsert, and the cap lives in a WHERE on the update
path. The insert path -- the one that runs when no row exists yet -- had no such check, so
the day's first request went through whatever the cap was. Two consequences, both real:

* any cap could be exceeded by exactly one request;
* a cap of 0 did not refuse, which is not a cap of 0. It is no cap at all until the second
  request of the day, and a disabled rung that still spends is worse than a disabled one.

These are real spends against the autouse SQLite `budget_counter` fixture, because the
bug is in the SQL and a mocked database cannot see SQL.
"""

import re

import pytest
from sqlalchemy.dialects.postgresql import asyncpg

from src.shared.budget import GROQ_REQUESTS, _SPEND, today, spend, used


class TestCapOnFirstSpend:
    @pytest.mark.asyncio
    async def test_a_cap_of_zero_refuses_the_first_spend(self):
        assert await spend(GROQ_REQUESTS, 1, 0) is None
        assert await used(GROQ_REQUESTS, day=today()) == 0

    @pytest.mark.asyncio
    async def test_a_cap_of_zero_refuses_every_spend(self):
        for _ in range(5):
            assert await spend(GROQ_REQUESTS, 1, 0) is None

    @pytest.mark.asyncio
    async def test_the_first_spend_is_still_bounded_by_the_cap(self):
        """A cap of 3 must refuse a 4th request, and the very first request is the one the
        insert path used to let through unconditionally."""
        for _ in range(3):
            assert await spend(GROQ_REQUESTS, 1, 3) is not None
        assert await spend(GROQ_REQUESTS, 1, 3) is None
        assert await used(GROQ_REQUESTS, day=today()) == 3

    @pytest.mark.asyncio
    async def test_a_single_request_larger_than_the_cap_is_refused(self):
        assert await spend(GROQ_REQUESTS, 5, 4) is None
        assert await used(GROQ_REQUESTS, day=today()) == 0

    @pytest.mark.asyncio
    async def test_a_spend_exactly_at_the_cap_is_allowed(self):
        """Off-by-one in the other direction is its own bug: refusing a spend that lands
        exactly on the cap silently costs a request."""
        assert await spend(GROQ_REQUESTS, 1, 1) == 1
        assert await spend(GROQ_REQUESTS, 1, 1) is None

    @pytest.mark.asyncio
    async def test_a_refused_first_spend_does_not_create_a_row(self):
        """Creating the row on a refused insert would make the next day's status report
        usage that never happened."""
        await spend(GROQ_REQUESTS, 1, 0)
        status = await used(GROQ_REQUESTS, day=today())
        assert status == 0


class TestTheStatementParsesOnTheDatabaseItActuallyRunsOn:
    """The gap that let the statement above ship broken for two hours on 2026-10-03.

    Everything else in this file runs the cap against SQLite, which is the one dialect
    that cannot see this defect: aiosqlite hands the bound values to the driver as they
    are and never asks the server to deduce a parameter's type. Postgres deduces each
    parameter's type at parse time and refuses the statement when two uses disagree --
    and in `INSERT ... SELECT :name, :day, :amount WHERE :amount <= :cap` they do, because
    the SELECT list types `:amount` as the target column (bigint) and the comparison
    types both of its operands as integer. The live error was

        ProgrammingError: inconsistent types deduced for parameter $3

    which `spend()` catches and reports as "cap spent", so the observable symptom was not
    a crash at all: on Postgres every daily budget read as spent and every LLM call was
    refused, with a green suite the whole way. Measured live on dev through the pg
    tunnel, postgresql+asyncpg.

    So the check here is the compiled SQL, not a mock: every numeric parameter must carry
    an explicit type at every use site, on the dialect production runs.
    """

    # NOTE: A generic-dialect test (postgresql.dialect()) was removed 2026-10-04.
    # It rendered psycopg2-style %(name)s placeholders, but production uses
    # asyncpg, which renders $N. The generic test passed on SQLAlchemy 2.1.1
    # and failed on the pinned 2.0.54, proving it was testing the wrong
    # dialect's rendering, not the production contract. The asyncpg test
    # below is the correct one: it asserts the same property (numeric params
    # carry explicit casts) against the driver production actually uses.
    def test_the_numeric_parameters_are_typed_for_the_driver_production_uses(self):
        """asyncpg renders bind parameters as $N, so the cast is the only thing carrying
        a type, and this is the driver the app and dev both connect with. Compiling with
        the generic dialect instead would miss a fix that only works for psycopg2.

        Asserted as a property of the whole statement rather than as a search for a
        parameter name: the cast is what matters, so every placeholder in the rendered
        SQL must either carry one or be `:name`/`:day`, which are typed by the columns
        they are compared against. Both casts have to be there on every use site, because
        one uncasted comparison is the whole bug.
        """
        compiled = str(_SPEND.compile(dialect=asyncpg.dialect()))
        uncast = set(re.findall(r"\$(\d+)(?!::)", compiled))
        # $1 is :name and $2 is :day -- the only two parameters Postgres can type for
        # itself. Everything numeric has to arrive already typed.
        assert uncast <= {"1", "2"}, f"untyped bind parameters ${{{uncast}}} in:\n{compiled}"
        # :amount is bound once and used four times (the SELECT list, the insert's own
        # cap check, and twice in the ON CONFLICT arm); :cap is used twice. Counting
        # the use sites catches a fix that casts one comparison and leaves the other,
        # which parses on Postgres and is still wrong.
        assert compiled.count("$3::BIGINT") == 4, compiled
        assert compiled.count("$4::BIGINT") == 2, compiled


class TestReportCapMatchesRuntimeCap:
    """The usage report must not disagree with the runtime about the cap.

    The CounterSpec cap_setting is what scripts/report_budget_usage.py shows.
    The llm_roster rung is what actually refuses calls. If they point at
    different settings, the report says 190% while the runtime says 95%.
    """

    def test_groq_token_cap_setting_matches_rung(self):
        from src.shared.budget import COUNTERS_BY_NAME, GROQ_REQUEST_TOKENS
        from src.shared.config import get_settings
        from src.shared.llm_roster import ROSTER

        spec = COUNTERS_BY_NAME[GROQ_REQUEST_TOKENS]
        # The report's cap must come from the same setting the rung enforces.
        assert spec.cap_setting == "groq_daily_token_cap", (
            f"CounterSpec uses {spec.cap_setting}, but the rung enforces "
            "groq_daily_token_cap"
        )
        # And the default must match, so a missing setting doesn't diverge.
        settings = get_settings()
        rung = next(r for r in ROSTER if r.name == "groq")
        assert spec.default_cap == getattr(settings, rung.daily_token_cap_attr), (
            f"CounterSpec default {spec.default_cap} != "
            f"rung default {getattr(settings, rung.daily_token_cap_attr)}"
        )
