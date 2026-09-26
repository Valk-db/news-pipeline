#!/usr/bin/env python
"""Claim extraction run: populate claims + claim_evidence for QUEUED stories.

Used by .github/workflows/weekly-claim-extraction.yml (or added to weekly-enrichment).

Usage:
    uv run python scripts/run_claim_extraction.py
"""

import asyncio
import json
import os

from src.shared.database import init_db, get_session_maker
from src.verification.claims import extract_claims_for_recent_stories


async def main() -> None:
    await init_db()
    session_factory = get_session_maker()
    results = await extract_claims_for_recent_stories(session_factory, hours_back=168, max_stories=100)
    print(f"Processed {len(results)} stories")

    total_claims = 0
    total_evidence = 0
    total_errors = 0
    for r in results:
        if r.get("errors"):
            print(f"  Story {r['story_id']}: {len(r['errors'])} errors")
            total_errors += len(r['errors'])
        else:
            print(
                f"  Story {r['story_id']}: claims={r['claims_created']} "
                f"evidence={r['evidence_created']}"
            )
            total_claims += r.get("claims_created", 0)
            total_evidence += r.get("evidence_created", 0)

    summary = {
        "stories_processed": len(results),
        "total_claims_created": total_claims,
        "total_evidence_created": total_evidence,
        "total_errors": total_errors,
    }
    summary_line = f"CLAIM_EXTRACTION_SUMMARY={json.dumps(summary)}"
    print(summary_line)

    # Also write it as a step output for the workflow's follow-up check.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(summary_line + "\n")


if __name__ == "__main__":
    asyncio.run(main())