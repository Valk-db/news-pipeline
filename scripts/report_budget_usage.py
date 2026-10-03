#!/usr/bin/env python
"""What did today cost, in the units the providers actually enforce?

Answers a question the existing counters could not: "which budgets are exhausted, and
what stopped them?" The reason that needed a new script is that the answer depends on the
UNIT. A request counter and a token counter can both be far from their caps while the
provider refuses every call, because Groq's free tier enforces tokens/day -- so printing
one number per counter, with its name as the only clue, is exactly what let a day read
"97% unspent" while the day's 200,000 tokens were gone.

So every counter is printed with its declared unit and its own cap, and the report
separates the case this batch exists to make visible:

    TOKEN-EXHAUSTED, REQUESTS UNDER

a counter whose tokens are at or over cap while its requests are still far under. That
combination is the signature of a request-denominated cap watching a token-denominated
limit, and it is the one state where "requests look fine" is actively misleading.

Reads budget_counters directly rather than going through src/shared/budget.py, because a
report that asks the module it is reporting on whether it can read its own data reports
"unreadable" in the same shape as "spent". The counters are small and this is a
diagnostic, so a plain read is the honest shape.

Usage:
    python -m scripts.report_budget_usage [--day YYYY-MM-DD] [--json] [--db-url URL]

Exits 0 normally. Exits 1 when the database cannot be reached, because a report that
prints a confident zero for an unreachable database is worse than no report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.budget import COUNTERS_BY_NAME, CounterSpec, counter_cap

# A counter whose unit is neither requests nor tokens is counted in whatever the provider
# enforces for it -- MyMemory is keyless and capped in characters -- so it is reported in
# its own unit and never compared against a token budget.
TOKEN_UNIT = "tokens"

# How far under the request cap a counter has to be before "tokens are exhausted but
# requests are not" is called out. The point is that the two caps are not comparable
# (900 requests and 60,000 tokens are different quantities), so this is a share of the
# REQUEST cap rather than an absolute token figure: under a tenth of the request cap with
# tokens spent is unambiguously the pathological shape, and a threshold that tried to be
# clever about the ratio would be harder to argue with than one that just says "far under".
REQUESTS_UNSHARE = 0.10


def is_exhausted(row: dict) -> bool | None:
    """Whether `row`'s counter is at or over its cap, or None if it cannot be told.

    One definition, used by both the table and the flag. An earlier version stored
    `exhausted` on the row and had two readers trust it, which is a stored boolean that can
    disagree with the numbers it was derived from -- and a stale one is invisible, because
    a confident "ok" is exactly what a wrong answer looks like here.
    """
    used, cap = row["used"], row["cap"]
    if used is None or cap is None:
        return None
    return used >= cap


def _row_for(name: str, used: int | None, cap: int, spec: CounterSpec) -> dict:
    pct = (used / cap * 100.0) if (used is not None and cap > 0) else None
    return {
        "name": name,
        "unit": spec.unit,
        "used": used,
        "cap": cap,
        "percent": pct,
        "spent_by": spec.spent_by,
    }


async def collect(db_url: str, day: date) -> list[dict]:
    """One row per known counter, plus any counter in the table we do not know about."""
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            found = dict(
                (
                    await conn.execute(
                        text("SELECT name, used FROM budget_counters WHERE day = :day"),
                        {"day": day},
                    )
                ).all()
            )
    finally:
        await engine.dispose()

    rows = []
    for name, spec in COUNTERS_BY_NAME.items():
        raw = found.pop(name, None)
        # No row means nothing was spent, which is ZERO -- not "unreadable". Conflating
        # the two made every untouched counter print as unreadable on an ordinary quiet
        # day, i.e. the report cried wolf on the healthy case and taught a reader to
        # ignore it. src/shared/budget.py's own used() makes the same distinction.
        rows.append(_row_for(name, 0 if raw is None else int(raw), counter_cap(spec), spec))
    # A counter this module has never heard of is still spend, and a report that silently
    # omits it would read as "nothing else happened today". Reported with no cap, because
    # inventing one here would be a guess dressed as a limit.
    for name in sorted(found):
        rows.append(
            {
                "name": name,
                "unit": "unknown",
                "used": int(found[name]),
                "cap": None,
                "percent": None,
                "spent_by": "NOT IN src/shared/budget.py COUNTERS -- undeclared counter",
            }
        )
    return rows


def flag_token_exhausted(rows: list[dict]) -> list[dict]:
    """Counters whose TOKENS are spent while their REQUESTS are far under.

    Pairs each token counter with the request counter that measures the same stage. The
    pairing comes from CounterSpec.pairs_with rather than from string surgery on the
    counter name, because the pair is not a naming convention (`groq_requests` pairs with
    `groq_request_tokens`) and a derived pairing is one rename away from silently
    comparing unrelated counters. This report's central claim is a claim about a PAIR.

    A token counter with no request counterpart (Phase 2 never had a request counter) is
    reported on its own rather than silently dropped.
    """
    by_name = {r["name"]: r for r in rows}
    flagged = []
    for name, row in by_name.items():
        # Recomputed here rather than read off the row's `exhausted` field, so this
        # function's answer depends only on the numbers it is given. A flag that trusts a
        # precomputed boolean is a flag that can be silently stale.
        if row["unit"] != TOKEN_UNIT or is_exhausted(row) is not True:
            continue
        spec = COUNTERS_BY_NAME.get(name)
        twin = by_name.get(spec.pairs_with) if spec and spec.pairs_with else None
        if (
            twin is not None
            and twin["used"] is not None
            and twin["cap"]
            and twin["used"] < twin["cap"] * REQUESTS_UNSHARE
        ):
            flagged.append({**row, "request_counter": twin})
    return flagged


def format_table(rows: list[dict], flagged: list[dict]) -> str:
    """The human-facing report. Every counter, its unit, and its own cap."""
    out: list[str] = []
    names = [r["name"] for r in rows]
    width = max([len(n) for n in names] + [20])
    out.append(f"{'COUNTER'.ljust(width)}  {'USED':>12}  {'CAP':>12}  {'%':>7}  STATE")
    out.append("-" * (width + 46))
    for r in rows:
        used = "unreadable" if r["used"] is None else f"{r['used']:,}"
        cap = "-" if r["cap"] is None else f"{r['cap']:,}"
        pct = "-" if r["percent"] is None else f"{r['percent']:.1f}%"
        exhausted = is_exhausted(r)
        if exhausted is True:
            state = f"EXHAUSTED ({r['unit']})"
        elif exhausted is False:
            state = "ok"
        else:
            state = "unknown"
        out.append(f"{r['name'].ljust(width)}  {used:>12}  {cap:>12}  {pct:>7}  {state}")

    out.append("")
    if flagged:
        out.append(
            f"!! {len(flagged)} counter(s) at or over their TOKEN cap while their request"
            " count is far under its cap."
        )
        out.append(
            "   Requests look healthy and the free tier is exhausted. Raising the request"
            " cap will not help;"
        )
        out.append("   the token cap is what binds. This is the state that reads as healthy.")
        for r in flagged:
            t = r["request_counter"]
            out.append(
                f"     - {r['name']}: {r['used']:,}/{r['cap']:,} tokens, while"
                f" {t['name']} is only {t['used']:,}/{t['cap']:,}"
            )
    else:
        out.append("No counter is token-exhausted while its request count is far under.")

    undeclared = [r for r in rows if r["unit"] == "unknown"]
    if undeclared:
        out.append("")
        out.append(
            f"!! {len(undeclared)} counter(s) in budget_counters are not declared in"
            " src/shared/budget.py COUNTERS."
        )
        out.append("   Their daily cost is not capped by this pipeline and cannot be reported in a unit.")
        for r in undeclared:
            out.append(f"     - {r['name']}: {r['used']:,}")
    return "\n".join(out)


async def _main_async(db_url: str, day: date, want_json: bool) -> int:
    rows = await collect(db_url, day)
    flagged = flag_token_exhausted(rows)
    if want_json:
        print(json.dumps({"day": str(day), "counters": rows, "token_exhausted": flagged}, indent=2))
    else:
        print(format_table(rows, flagged))
        print("")
        print(f"day: {day} (UTC)")
    return 0


def _resolve_db_url(explicit: str | None) -> str:
    """The database to read. Never printed, never logged."""
    if explicit:
        return explicit
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set; pass --db-url or set it in the environment")
    # The pooler URL cannot serve this read the same way a direct connection does, and
    # the sync/async driver choice has to match the scheme. Left as the app's own
    # prepare_database_url wherever possible rather than guessed at here.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", help="UTC day to report (YYYY-MM-DD); defaults to today")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--db-url", help="override DATABASE_URL")
    args = parser.parse_args(argv)

    if args.day:
        day = date.fromisoformat(args.day)
    else:
        # The same UTC-day rule src/shared/budget.py:today() applies to the counters, so
        # the report cannot disagree with the rows about which day they belong to.
        day = datetime.now(timezone.utc).date()

    try:
        db_url = _resolve_db_url(args.db_url)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        return asyncio.run(_main_async(db_url, day, args.json))
    except Exception as exc:
        # No confident zero. A report that prints "0 used" for an unreachable database is
        # the failure mode this script exists to prevent, so it exits non-zero instead.
        print(f"could not read budget_counters: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())