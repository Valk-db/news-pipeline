"""Daily spend counters, shared by every process that spends a provider's daily quota.

Both budgets used to be per-process: an int in llm_budget.py, a JSON file under var/ on
a GitHub Actions runner. Neither survives the process, and the runner does not survive the
job, so a cap written as "900 Groq requests a day" was in practice 900 *per run* against a
key used twice a day. A row per (name, day) makes the cap a fact about the pipeline.

One statement does the whole job::

    INSERT INTO budget_counters (name, day, used) VALUES (:name, :day, :amount)
    ON CONFLICT (name, day) DO UPDATE
        SET used = budget_counters.used + :amount
        WHERE budget_counters.used + :amount <= :cap
    RETURNING used

    A plain UPDATE would be atomic too, but there is no row to update until the day's first
    spend, so the insert path has to exist; putting both in one statement is what keeps the
    cap honest with two runners in flight. The WHERE on the update path is what makes the
    answer the caller gets *the* answer: no row returned means the increment would have passed
    the cap, so nothing was spent.

    The insert path needs its own WHERE for the same reason, and it is easy to leave off
    because it looks redundant next to the one below it. Without it the cap is not applied
    to the day's first spend at all: the row is created with `used = :amount` no matter
    what :cap is. The visible consequences are a cap that can be exceeded by exactly one
    request, and a cap of 0 that does not refuse -- which is not a cap of 0, it is no cap
    at all until the second request of the day.


Degradation is deliberately one-directional. If the database is unreachable, or the table
is missing, or nothing is configured, spend() returns None -- the same answer as "over
cap" -- because the one thing this module must never do is let a caller spend because it
could not check. A budget that cannot be verified is treated as spent.

That one-directional rule has a limit, and finding it cost two hours of refused LLM calls
on 2026-10-03. Every exception this function catches maps onto the same fail-safe answer,
so a DEFECT in the statement above is indistinguishable at the call site from a genuinely
exhausted cap. A commit rewrote _SPEND into an `INSERT .. SELECT .. WHERE :amount <= :cap`
form; on Postgres that does not parse (`:amount` is bigint in the SELECT list but integer
in the comparison, so the server raises `ProgrammingError: inconsistent types deduced for
parameter $3`). spend() swallowed it, logged "unreachable, treating 1 as spent", and
returned None -- which every caller reads as *cap spent*. Every daily budget on the
pipeline read as exhausted and every LLM call was refused for two hours, with no crash
anywhere to find. So the catch is now split: an operational failure (the server is down,
the connection died, the migration has not been applied) still fails safe, and a
ProgrammingError -- which is never transient, and can only mean the statement or the
schema is wrong -- propagates. A loud failure in one run beats a silent refusal in all of
them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, UTC

from sqlalchemy import BigInteger, Date, bindparam, text
from sqlalchemy.exc import ProgrammingError

from src.shared.database import _get_engine

logger = logging.getLogger(__name__)

# Budget names, one row per name per day. Named for what is spent, not who spends it.
GROQ_REQUESTS = "groq_requests"
# Translation's own row, not GROQ_REQUESTS: it is best-effort enrichment that runs
# after the LLM work, and sharing one cap would let a long translation batch starve
# caption/classification work that ran first (or the reverse).
GROQ_TRANSLATION_REQUESTS = "groq_translation_requests"
MYMEMORY_CHARS = "mymemory_chars"
# Cerebras was the one rung with no counter at all: it fell through from Groq and spent
# whatever the provider allowed, so a Cerebras-only day was uncountable. Same rule as
# GROQ_REQUESTS vs GROQ_TRANSLATION_REQUESTS -- one row per rung, so a rung's exhaustion
# is its own news.
CEREBRAS_REQUESTS = "cerebras_requests"
# One row per free MODEL, not per provider. OpenRouter's ":free" pool is shared across
# every OpenRouter user, so google/gemma-4-26b-a4b-it:free and
# nvidia/nemotron-3-super-120b-a12b:free get 429'd independently: on 2026-10-03 Gemma
# returned 429 on 3/3 attempts while Nemotron returned 200 on the first. A single
# openrouter_requests row would let that outage report itself as "the budget is spent"
# and demote a working model.
OPENROUTER_GEMMA_REQUESTS = "openrouter_gemma_requests"
OPENROUTER_NEMOTRON_REQUESTS = "openrouter_nemotron_requests"

# Naming is a function, not a convention, for the reason in the token-counter
# block below: a hand-written token name can drift from its request name, and the
# drift is silent -- two rungs end up sharing a token row, or one rung's spend
# lands in a row nobody reads. Deriving both from the same string makes that
# impossible to write by accident. `groq_requests` -> `groq_tokens`.
def token_counter_name(request_counter: str) -> str:
    """The token-denominated counterpart of a request counter's row name.
    
    "groq_requests" -> "groq_request_tokens" (keeps singular "request" to match
    the explicit GROQ_REQUEST_TOKENS constant).
    """
    if request_counter.endswith("_requests"):
        # Remove just the trailing 's': "groq_requests" -> "groq_request" + "_tokens"
        return request_counter[:-1] + "_tokens"
    return request_counter + "_tokens"


def unpriced_calls_counter_name(token_counter: str) -> str:
    """The row that counts calls whose token cost could not be read.

    A separate row rather than a flag on the token row, because the token row is
    a quantity in the provider's unit and this is a count of calls. Recording a
    missing `usage` as zero would be the dishonest answer -- zero is a claim that
    the call was free, and a wrong zero is how a cap silently stops capping. A
    separate count makes "this day is 12,400 tokens across 41 calls, 3 of which
    could not be priced" answerable, instead of the token figure silently being
    a lower bound with nothing to say so.
    """
    return (token_counter[:-len("_tokens")] if token_counter.endswith("_tokens")
            else token_counter) + "_unpriced_calls"

# Phase 2 (claim extraction on gate-passed PENDING stories, src/verification/phase2.py)
# counts TOKENS, not requests, and that is not a preference.
#
# Groq's free plan for the model this pipeline uses (openai/gpt-oss-20b) publishes
# RPM 30 / RPD 1,000 / TPM 8,000 / TPD 200,000 (checked against
# https://console.groq.com/docs/rate-limits on 2026-10-03). A request-count cap
# cannot see the limit that actually binds: one claim extraction measured 4,962
# total tokens against a 200,000/day allowance, so 40 such calls exhaust the day's
# tokens while a 1,000-request cap would still read 96% unspent. The daily limiter
# for this provider is its token allowance, so this counter is denominated in
# tokens and the recorded amount is the `usage.total_tokens` the provider reported.
#
# Its own row for the same reason translation has one: Phase 2 is the largest
# single consumer of the free tier in this pipeline, and sharing GROQ_REQUESTS
# would make caption/classification work silently disappear on the day Phase 2
# filled the shared counter.
GROQ_PHASE2_TOKENS = "groq_phase2_tokens"

# Token-denominated counterparts of the request counters above, one per rung.
#
# The Groq token counters use explicit names (not derived) because they are
# documented in AGENTS.md and the pairing is declared as data in CounterSpec
# below, not inferred from naming. For newer rungs (Cerebras, OpenRouter),
# token_counter_name() derives the name from the request counter so the pair
# cannot drift.
#
# Why a token counter is not redundant with the request one: on 2026-10-03 dev's
# `groq_requests` read 28 against a fully spent 200,000-token day, the crossing
# request reading `TPD: Limit 200000, Used 199337, Requested 4388`. A
# request-denominated cap read 97% unspent while the quota was gone. Both limits
# are real and they have different numbers, so both are counted and both are
# capped.
#
# `GROQ_PHASE2_TOKENS` above is deliberately NOT this mechanism: Phase 2 calls
# Groq through src/verification/claims.py, not through LLMClient._walk, so it
# never reaches the code that records these. Sharing one row would have made
# Phase 2's spend visible to the roster's cap, which is the starvation the
# separate row exists to prevent.

# Explicit names for the Groq pairs (documented in AGENTS.md).
GROQ_REQUEST_TOKENS = "groq_request_tokens"
GROQ_TRANSLATION_TOKENS = "groq_translation_tokens"
# Backwards compatibility alias: HEAD's code used GROQ_TOKENS for the request token counter.
GROQ_TOKENS = GROQ_REQUEST_TOKENS

# Derived names for the newer rungs via token_counter_name() above.
CEREBRAS_TOKENS = token_counter_name(CEREBRAS_REQUESTS)
OPENROUTER_GEMMA_TOKENS = token_counter_name(OPENROUTER_GEMMA_REQUESTS)
OPENROUTER_NEMOTRON_TOKENS = token_counter_name(OPENROUTER_NEMOTRON_REQUESTS)


@dataclass(frozen=True)
class CounterSpec:
    """What one budget counter measures, so nothing has to guess from its name.

    `unit` is the reason this class exists. A counter called `groq_requests` looks like a
    requests counter, and `mymemory_chars` looks like a chars counter, so a reader infers
    the denomination from the name and never asks whether it matches what the provider
    enforces. Declaring the unit once, next to the cap that is denominated in it, is what
    lets the daily report say "this counter is exhausted and here is why" instead of
    printing a number whose meaning depends on its spelling.
    """

    name: str
    unit: str  # "requests" | "tokens" | "chars"
    cap_setting: str  # attribute on Settings holding the daily cap
    default_cap: int
    spent_by: str  # which stage pays this row
    # The other counter measuring the SAME calls in the other unit, or None. Declared
    # rather than derived from the name because the pair is not a naming convention:
    # `groq_requests` pairs with `groq_request_tokens`, and a rule that inferred it from
    # the string would have to special-case the singular/plural mismatch and would then
    # be one rename away from silently pairing the wrong counters. The daily report's
    # central claim -- tokens spent while requests look fine -- is a claim about a PAIR,
    # so the pairing is data.
    pairs_with: str | None = None


# Every counter the pipeline can spend against, in one place.
#
# This is a registry rather than a comment because the daily report has to answer "what did
# today cost, and which counters are exhausted" for a counter it did not hardcode, and
# because a new rung that is not listed here is a counter whose daily cost is invisible --
# which is the defect this batch exists to fix. A test asserts that every counter named in
# this module appears here, so a new spend() call site cannot be added without a row.
COUNTERS: tuple[CounterSpec, ...] = (
    CounterSpec(
        name=GROQ_REQUESTS,
        unit="requests",
        cap_setting="groq_daily_request_budget",
        default_cap=900,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=GROQ_REQUEST_TOKENS,
    ),
    CounterSpec(
        name=GROQ_REQUEST_TOKENS,
        unit="tokens",
        cap_setting="groq_daily_token_cap",
        default_cap=120_000,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=GROQ_REQUESTS,
    ),
    CounterSpec(
        name=GROQ_TRANSLATION_REQUESTS,
        unit="requests",
        cap_setting="groq_translation_daily_request_budget",
        default_cap=300,
        spent_by="translation (src/enrichment/translation.py)",
        pairs_with=GROQ_TRANSLATION_TOKENS,
    ),
    CounterSpec(
        name=GROQ_TRANSLATION_TOKENS,
        unit="tokens",
        cap_setting="groq_translation_daily_token_budget",
        default_cap=20_000,
        spent_by="translation (src/enrichment/translation.py)",
        pairs_with=GROQ_TRANSLATION_REQUESTS,
    ),
    CounterSpec(
        name=GROQ_PHASE2_TOKENS,
        unit="tokens",
        cap_setting="phase2_daily_token_cap",
        default_cap=40_000,
        spent_by="Phase 2 claim extraction (src/verification/phase2.py)",
    ),
    CounterSpec(
        name=MYMEMORY_CHARS,
        unit="chars",
        cap_setting="mymemory_daily_char_budget",
        default_cap=45_000,
        spent_by="translation (src/enrichment/translation.py)",
    ),
    CounterSpec(
        name=CEREBRAS_REQUESTS,
        unit="requests",
        cap_setting="cerebras_daily_request_budget",
        default_cap=900,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=CEREBRAS_TOKENS,
    ),
    CounterSpec(
        name=CEREBRAS_TOKENS,
        unit="tokens",
        cap_setting="cerebras_daily_token_budget",
        default_cap=60_000,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=CEREBRAS_REQUESTS,
    ),
    CounterSpec(
        name=OPENROUTER_GEMMA_REQUESTS,
        unit="requests",
        cap_setting="openrouter_gemma_daily_request_budget",
        default_cap=900,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=OPENROUTER_GEMMA_TOKENS,
    ),
    CounterSpec(
        name=OPENROUTER_GEMMA_TOKENS,
        unit="tokens",
        cap_setting="openrouter_gemma_daily_token_budget",
        default_cap=60_000,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=OPENROUTER_GEMMA_REQUESTS,
    ),
    CounterSpec(
        name=OPENROUTER_NEMOTRON_REQUESTS,
        unit="requests",
        cap_setting="openrouter_nemotron_daily_request_budget",
        default_cap=900,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=OPENROUTER_NEMOTRON_TOKENS,
    ),
    CounterSpec(
        name=OPENROUTER_NEMOTRON_TOKENS,
        unit="tokens",
        cap_setting="openrouter_nemotron_daily_token_budget",
        default_cap=60_000,
        spent_by="caption / classification (src/shared/llm_budget.py)",
        pairs_with=OPENROUTER_NEMOTRON_REQUESTS,
    ),
)

COUNTERS_BY_NAME = {spec.name: spec for spec in COUNTERS}


def counter_cap(spec: CounterSpec) -> int:
    """The configured daily cap for `spec`, falling back to its documented default.

    Settings are read here rather than at import time because a budget cap that cannot be
    read must not become an unlimited one, and an import-time read would freeze whatever
    the environment happened to hold when the module was first imported.
    """
    from src.shared.config import get_settings

    value = getattr(get_settings(), spec.cap_setting, None)
    return spec.default_cap if not isinstance(value, int) or value <= 0 else value


_SPEND = text(
    """
    INSERT INTO budget_counters (name, day, used)
    SELECT :name, :day, :amount WHERE :amount <= :cap
    ON CONFLICT (name, day) DO UPDATE
        SET used = budget_counters.used + :amount
        WHERE budget_counters.used + :amount <= :cap
    RETURNING used
    """
).bindparams(
    # The numeric and date parameters are TYPED, and that is load-bearing rather than
    # tidiness. Postgres deduces a parameter's type at parse time and refuses the
    # statement outright when two uses of one parameter deduce differently: in the
    # SELECT list `:amount` is the target column (bigint), while in `:amount <= :cap`
    # both sides are untyped literals, which Postgres resolves as integer. The
    # statement therefore never parsed on Postgres -- it raised
    # `ProgrammingError: inconsistent types deduced for parameter $3` -- and every
    # spend() on a real database returned None, which this module reads as "cap
    # spent" and which RequestBudget reads as "do not spend". Measured live on dev
    # 2026-10-03 through the pg tunnel, against postgresql+asyncpg.
    #
    # SQLite is why 2,000+ tests could not see it: aiosqlite passes the bound values
    # straight through and never asks Postgres-style type inference to happen, so the
    # whole suite was green against a statement that cannot run where it runs in
    # production. Every numeric parameter is cast at every use site by this
    # bindparam, which is the one shape both dialects accept. Naming the types
    # removes the inference entirely.
    bindparam("amount", type_=BigInteger),
    bindparam("cap", type_=BigInteger),
    bindparam("day", type_=Date),
)

_USED = text("SELECT used FROM budget_counters WHERE name = :name AND day = :day").bindparams(
    bindparam("day", type_=Date),
)

# The same upsert with NO cap predicate, for accounting rather than reservation.
#
# This exists because the two are different acts and conflating them loses money. spend()
# answers "may I afford this?" and is entitled to refuse. record() answers "this happened"
# and must never refuse, because by the time record() is called the tokens are already
# gone: with the capped form, a charge made after the cap was reached matched no row,
# spend() returned None, and the counter stayed frozen at exactly the cap -- so the daily
# report showed 60,000/60,000 for a day that actually cost more, and the overshoot was
# invisible precisely when it mattered. A cap that can hide its own overshoot is not a cap.
#
# Explicit bind types for the same reason as _SPEND: the statement is parsed by the same
# server, and "inconsistent types deduced for parameter" is not a dialect curiosity.
_RECORD = text(
    """
    INSERT INTO budget_counters (name, day, used) VALUES (:name, :day, :amount)
    ON CONFLICT (name, day) DO UPDATE
        SET used = budget_counters.used + :amount
    RETURNING used
    """
).bindparams(
    bindparam("amount", type_=BigInteger),
    bindparam("day", type_=Date),
)


def today() -> date:
    """The UTC day a spend belongs to. One definition, so no caller can drift."""
    return datetime.now(UTC).date()


async def spend(name: str, amount: int, cap: int, *, day: date | None = None) -> int | None:
    """Reserve `amount` against today's `cap` for `name`.

    Returns the new total, or None when the reservation was refused: the cap is already
    spent, or the counter could not be reached. Callers must treat None as "do not spend".

    Raises ProgrammingError rather than returning None for it. Every caller reads None as
    "cap spent", so mapping a broken statement onto None does not fail safe -- it fails
    *silently and completely*, which is the two-hour outage in the module docstring. A
    ProgrammingError means the server parsed our SQL and rejected it, which is a defect in
    this module or a schema that does not match the migration; it is never transient, and
    retrying or hiding it converts a bug that would take one run to diagnose into a
    condition that reads as a healthy exhausted budget forever.
    """
    # Defense in depth: the _SPEND SQL enforces the cap on both INSERT and UPDATE paths
    # via the WHERE clause. This Python guard provides an early return without a database
    # round-trip when the amount obviously exceeds the cap. It is safe for all callers:
    # TokenBudget.record uses _NO_GATE (2**62) which no real amount can exceed, and
    # RequestBudget._counted uses the real daily_limit where amount=1.
    if amount > cap:
        return None
    engine = _get_engine()
    if engine is None:
        logger.warning("budget %s: no database engine, treating %s as spent", name, amount)
        return None
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(
                _SPEND, {"name": name, "day": day or today(), "amount": amount, "cap": cap}
            )).first()
            await conn.commit()
    except ProgrammingError:
        # Deliberately not caught into the fail-safe below. See the docstring.
        logger.error(
            "budget %s: the counter statement was rejected by the server. This is a "
            "defect, not an exhausted budget, and it is raised rather than reported as "
            "'spent' so it cannot present as a quiet refusal. Amount %s, cap %s.",
            name, amount, cap,
        )
        raise
    except Exception as exc:
        # Fail safe, and ONLY for operational failures: the server is unreachable, the
        # connection dropped, the migration has not been applied. An unreadable counter
        # must not become an unlimited one.
        logger.warning("budget %s unreachable, treating %s as spent: %s", name, amount, type(exc).__name__)
        return None
    return None if row is None else int(row[0])


async def used(name: str, *, day: date | None = None) -> int | None:
    """What has been spent against `name` today, or None if it cannot be read."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(_USED, {"name": name, "day": day or today()})).first()
    except ProgrammingError:
        logger.error(
            "budget %s: the used statement was rejected by the server. This is a "
            "defect, not an unreadable budget, and it is raised rather than reported as "
            "'unreadable'.",
            name,
        )
        raise
    except Exception as exc:
        logger.warning("budget %s unreadable: %s", name, type(exc).__name__)
        return None
    return 0 if row is None else int(row[0])


async def record(name: str, amount: int, *, day: date | None = None) -> int | None:
    """Add `amount` to `name` unconditionally. Returns the new total, or None if the
    counter could not be reached.

    For spend that has ALREADY happened and is only now being measured -- a provider's
    reported token usage, chiefly. Deliberately has no `cap` parameter: the cap decides
    whether a call may be dispatched, which is spend()'s job, and it has no business
    deciding whether a completed call gets written down.

    This lets `used()` exceed a counter's cap. That is the point: an overshoot is a fact
    about the day, and a counter that pins itself at its cap reports a cost the pipeline
    did not incur.
    """
    amount = max(0, int(amount))
    engine = _get_engine()
    if engine is None:
        logger.warning("budget %s: no database engine, lost %s recorded", name, amount)
        return None
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(
                _RECORD, {"name": name, "day": day or today(), "amount": amount}
            )).first()
            await conn.commit()
    except ProgrammingError:
        logger.error(
            "budget %s: the record statement was rejected by the server. This is a "
            "defect, not an exhausted budget, and it is raised rather than reported as "
            "'spent'. Amount %s.", name, amount,
        )
        raise
    except Exception as exc:
        logger.warning("budget %s unreachable, lost %s of record: %s", name, amount, type(exc).__name__)
        return None
    return None if row is None else int(row[0])


def spend_sync(name: str, amount: int, cap: int, *, day: date | None = None) -> int | None:
    """spend() for the synchronous callers (src/enrichment/translation.py).

    Those run inside the pipeline's event loop, so they are called from a worker thread
    (see src/ingestion/run.py) where there is no loop to await on. The engine is built
    with NullPool, so a connection is never carried across the two loops.
    """
    return asyncio.run(spend(name, amount, cap, day=day))


def record_sync(name: str, amount: int, *, day: date | None = None) -> int | None:
    """record() for the same synchronous callers, on the same reasoning as spend_sync()."""
    return asyncio.run(record(name, amount, day=day))