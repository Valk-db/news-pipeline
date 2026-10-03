#!/usr/bin/env python
"""Answer "what did the roster cost today" from budget_counters, per rung.

The question was unaskable before 2026-10-03, and the reason it was unaskable is the
reason this script exists. `budget_counters` held request counts and one token row
(Phase 2's), so the only figure available for the roster was "28 requests". That is not
a cost: the same 28 requests were joined by a full 200,000-token day against Groq's
published TPD, which is the limit that actually binds and the one a request counter cannot
see. Rungs also shared a row in places, so even the request count was not per-provider.

The script is a thin wrapper over `src.shared.llm_roster.daily_roster_cost`, deliberately
so: it reads one table with columns that already exist, and it never edits anything. A
report that could fix the numbers it reports would be a report nobody could trust.

Read-only by construction, and it says so in its own output -- an operator reading
"0 tokens" needs to be able to tell "spent nothing" from "the query failed", so an
unreadable counter prints as ``unreadable`` and exits 2. A rung whose token figure is
below the truth (calls whose ``usage`` the provider did not report) prints the count of
those calls and marks the figure a LOWER BOUND, because a silent floor is how a cap stops
capping.

Usage::

    uv run python scripts/report_daily_cost.py            # today (UTC)
    uv run python scripts/report_daily_cost.py 2026-10-03 # a named UTC day
    uv run python scripts/report_daily_cost.py --json     # machine-readable
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root, so `src` is
# unimportable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared.config import get_settings
from src.shared.llm_roster import daily_roster_cost


def _day(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--day must be YYYY-MM-DD, got {raw!r}") from None


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", nargs="?", default=None,
                        help="UTC day to report on, YYYY-MM-DD (default: today)")
    parser.add_argument("--json", action="store_true",
                        help="Emit the per-rung breakdown as JSON instead of a table")
    args = parser.parse_args(argv)

    cost = await daily_roster_cost(day=_day(args.day), settings=get_settings())
    if args.json:
        print(json.dumps({
            "day": cost.day,
            "rungs": [
                {"rung": r.rung, "requests": r.requests, "request_cap": r.request_cap,
                 "tokens": r.tokens, "token_cap": r.token_cap,
                 "unpriced_calls": r.unpriced_calls,
                 "spend_is_lower_bound": r.spend_is_lower_bound}
                for r in cost.rungs
            ],
            "unreadable": any(r.tokens is None for r in cost.rungs),
            "counters_not_in_the_roster": list(cost.unknown_counters),
        }, indent=2))
    else:
        print(cost.render())

    # 2, not 0: "could not read the counters" and "the roster spent nothing" call for
    # different responses, and only one of them is a quiet day.
    return 2 if any(r.tokens is None for r in cost.rungs) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))