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

_SPEND = text(
    """
    INSERT INTO budget_counters (name, day, used) VALUES (:name, :day, :amount)
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