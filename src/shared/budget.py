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
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from sqlalchemy import text

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
    """The token-denominated counterpart of a request counter's row name."""
    return (request_counter[:-len("_requests")] if request_counter.endswith("_requests")
            else request_counter) + "_tokens"


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
# NOT DERIVED BY HAND, and not a second table. `budget_counters` is
# `(name TEXT NOT NULL, day DATE, used BIGINT)` with no CHECK on `name` and no
# foreign key to a budget registry, so a new name is free and a schema change is
# not: token_counter_name() below computes each name from the rung's existing
# request counter, which means two rungs cannot collide on tokens unless they
# already collide on requests -- the exact defect batch-freemodel fixed for
# requests (one shared row let one provider's spend silently disable another's)
# cannot come back through this door.
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
# separate row exists to prevent. The two counters are two different callers of
# one provider allowance, and see the config comments for how the arithmetic is
# sized so their sum stays under the provider's limit.

CEREBRAS_TOKENS = token_counter_name(CEREBRAS_REQUESTS)
OPENROUTER_GEMMA_TOKENS = token_counter_name(OPENROUTER_GEMMA_REQUESTS)
OPENROUTER_NEMOTRON_TOKENS = token_counter_name(OPENROUTER_NEMOTRON_REQUESTS)
GROQ_TOKENS = token_counter_name(GROQ_REQUESTS)

_SPEND = text(
    """
    INSERT INTO budget_counters (name, day, used)
    SELECT :name, :day, :amount WHERE :amount <= :cap
    ON CONFLICT (name, day) DO UPDATE
        SET used = budget_counters.used + :amount
        WHERE budget_counters.used + :amount <= :cap
    RETURNING used
    """
)

_USED = text("SELECT used FROM budget_counters WHERE name = :name AND day = :day")


def today() -> date:
    """The UTC day a spend belongs to. One definition, so no caller can drift."""
    return datetime.now(timezone.utc).date()


async def spend(name: str, amount: int, cap: int, *, day: date | None = None) -> int | None:
    """Reserve `amount` against today's `cap` for `name`.

    Returns the new total, or None when the reservation was refused: the cap is already
    spent, or the counter could not be reached. Callers must treat None as "do not spend".
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
    except Exception as exc:
        # Fail safe: an unreadable counter must not become an unlimited one.
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


def spend_sync(name: str, amount: int, cap: int, *, day: date | None = None) -> int | None:
    """spend() for the synchronous callers (src/enrichment/translation.py).

    Those run inside the pipeline's event loop, so they are called from a worker thread
    (see src/ingestion/run.py) where there is no loop to await on. The engine is built
    with NullPool, so a connection is never carried across the two loops.
    """
    return asyncio.run(spend(name, amount, cap, day=day))