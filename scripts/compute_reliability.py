#!/usr/bin/env python
"""Compute per-(source, topic) reliability scores from claim-evidence consensus.

See AGENT_TASKS.md P3-B for design rationale (claim-consensus based, not fact-check-verdict based).

Usage:
    uv run python scripts/compute_reliability.py
"""

import asyncio
import json
import os

from src.shared.database import init_db, get_session_maker
from src.verification.reliability import compute_source_topic_reliability


async def main() -> None:
    await init_db()
    session_factory = get_session_maker()
    async with session_factory() as session:
        results = await compute_source_topic_reliability(session, lookback_days=90)
    print(f"Pairs scored: {results['pairs_scored']}")
    print(f"Claims considered: {results['claims_considered']}")

    if results.get("errors"):
        print(f"Errors: {results['errors']}")

    summary = {
        "pairs_scored": results["pairs_scored"],
        "claims_considered": results["claims_considered"],
        "errors": results.get("errors", []),
    }
    summary_line = f"RELIABILITY_SUMMARY={json.dumps(summary)}"
    print(summary_line)

    # Also write it as a step output for the workflow's follow-up check.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(summary_line + "\n")


if __name__ == "__main__":
    asyncio.run(main())