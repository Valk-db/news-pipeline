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
from datetime import date, datetime, timezone

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

# Token-denominated counterparts to the two request counters above.
#
# Both of those count requests against a free tier whose binding limit is tokens/day, so
# each one read as almost entirely unspent on a day the allowance was gone. The measured
# case: at the end of a session `groq_requests` read 28 against a fully spent 200,000-token
# allowance -- 97% "unspent" -- and the request that crossed the line was a 429 reading
# "TPD: Limit 200000, Used 199337, Requested 4388". A request counter cannot see that
# limit, so the request caps stay (they are what the providers publish as well, and other
# stages spend against them) and these are added alongside.
#
# Separate names, not one shared "groq_tokens", for the reason the request counters are
# separate: caption/classification, translation and Phase 2 are different consumers that
# fail differently, and a shared token row would let the largest of them hide the others'
# exhaustion. That is not hypothetical here -- on the measured day Phase 2 alone cost
# 2,437 tokens per story, so a shared row would have been dominated by one stage within a
# handful of calls.
GROQ_REQUEST_TOKENS = "groq_request_tokens"
GROQ_TRANSLATION_TOKENS = "groq_translation_tokens"


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
        cap_setting="groq_daily_token_budget",
        default_cap=60_000,
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
    INSERT INTO budget_counters (name, day, used) VALUES (:name, :day, :amount)
    ON CONFLICT (name, day) DO UPDATE
        SET used = budget_counters.used + :amount
        WHERE budget_counters.used + :amount <= :cap
    RETURNING used
    """
).bindparams(
    # Every numeric and date parameter carries an explicit type, at every use site, and
    # that is load-bearing rather than decoration. `:amount` appears twice in this
    # statement -- once in the VALUES list, where Postgres types it from the target
    # column as bigint, and once inside the comparison, where both operands are untyped
    # and resolve as integer. Left to inference that is the "inconsistent types deduced
    # for parameter" failure described in the module docstring, which cost two hours of
    # refused calls before it was found. Naming the types removes the inference entirely.
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
    return datetime.now(timezone.utc).date()


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