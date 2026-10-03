#!/usr/bin/env python
"""Phase 2: run topic grouping, narrative arcs and claim extraction on gate-passed stories.

The daily job. `.github/workflows/daily-phase2.yml` calls this once a day, after both
ingests (`daily-ingest.yml` runs at `23 6,18 * * *`).

What it does, in order, for each selected story:

1. topic groups  -- derived from canonical entity names against TOPIC_KEYWORDS. No LLM.
2. narrative arcs -- derived from primary_entity overlap. No LLM.
3. claims        -- one LLM call, and the only thing here that costs money.

What it does not do, deliberately: it never changes a story's status, never writes a
`curated_posts` row, and never touches a public filter. A PENDING story stays PENDING and
keeps 404ing on every public route after this runs -- `tests/test_phase2_isolation.py`
is the test that holds that line. Public exposure remains a human decision.

The run does not fail. Every per-story failure mode is a recorded skip:
`already_analysed`, `budget_exhausted`, `rate_limited`, `out_of_credit`,
`too_few_units`, `error`.
A daily enrichment job that fails because a free tier said 429 teaches its operators to
ignore it. The one exception is a wholesale LLM auth failure (see below), which is a
misconfiguration rather than a transient provider state and is worth a red build.

Usage:
    uv run python scripts/run_phase2.py
    uv run python scripts/run_phase2.py --hours-back 48 --max-stories 16
    uv run python scripts/run_phase2.py --dry-run     # select and report, spend nothing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from src.shared.config import get_settings
from src.shared.database import get_session_maker
from src.shared.llm import LLMClient
from src.verification import claims as claims_stage
from src.verification.narrative import link_narrative_arcs
from src.verification.phase2 import (
    SKIP_ALREADY_ANALYSED,
    SKIP_BUDGET,
    SKIP_ERROR,
    SKIP_OUT_OF_CREDIT,
    SKIP_RATE_LIMITED,
    SKIP_TOO_FEW_UNITS,
    Phase2BudgetRefused,
    Phase2TokenBudget,
    new_result,
    select_phase2_stories,
    summarize,
)
from src.verification.topics import assign_story_topic_groups

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_phase2")

# Errors that mean "the key is wrong", not "the provider is busy". A run where most
# stories fail this way is a misconfiguration and is worth failing the build over; a
# single 401 among sixteen stories is a blip and is recorded like anything else.
AUTH_ERROR_MARKERS = ("401", "403", "unauthorized", "authentication", "invalid api key")


class _Skip(Exception):
    """Not an error: this story is not being worked on, and here is why."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _is_auth_error(detail: str) -> bool:
    low = detail.lower()
    return any(marker in low for marker in AUTH_ERROR_MARKERS)


async def _extract_claims(session_factory, story_id, *, budget, max_units, retries) -> dict:
    """Claim extraction with a single honoured retry on a real 429.

    The retry re-enters extract_claims_for_story() rather than retrying the HTTP call
    inside it, so the re-attempt passes the budget gate again. That costs one counter
    read and it is worth it: a retry that skipped the gate would be the one path in this
    file that could spend past the cap.

    A 402 is raised straight through as a skip. It is not retried and not routed around
    -- a 402 means a paid tier is configured somewhere, and quietly falling back to a
    free provider to keep a $0 job green would hide that.
    """
    for attempt in range(retries + 1):
        try:
            async with session_factory() as session:
                return await claims_stage.extract_claims_for_story(
                    session, story_id, max_units=max_units, budget=budget
                )
        except Phase2BudgetRefused as exc:
            raise _Skip(SKIP_BUDGET, str(exc)) from exc
        except Exception as exc:
            if LLMClient.is_out_of_credit(exc):
                raise _Skip(SKIP_OUT_OF_CREDIT, f"{type(exc).__name__}: {exc}") from exc
            if LLMClient.is_rate_limited(exc) and attempt < retries:
                wait = LLMClient._retry_after_seconds(
                    exc, get_settings().phase2_retry_after_seconds
                )
                logger.info(
                    "%s rate limited, retrying in %.1fs (attempt %d/%d)",
                    story_id, wait, attempt + 1, retries,
                )
                await asyncio.sleep(wait)
                continue
            if LLMClient.is_rate_limited(exc):
                raise _Skip(SKIP_RATE_LIMITED, f"{type(exc).__name__}: {exc}") from exc
            raise


async def run_story(session_factory, story_id, *, budget, units, retries, dry_run) -> dict:
    """One story's whole Phase 2 pass. Returns its run-record row."""
    result = new_result(story_id)

    # Stages 1 and 2 are derived state, not model output: keyword matching over
    # canonical entity names and set overlap. Both check before inserting, so a
    # re-run of this job creates nothing new for them.
    try:
        async with session_factory() as session:
            groups = await assign_story_topic_groups(session, story_id)
        result["topics_created"] = sum(1 for g in groups if g.get("created"))
    except Exception as exc:
        result["errors"].append(f"topic groups: {type(exc).__name__}: {exc}")
        logger.warning("topic groups failed for %s: %s", story_id, exc)

    try:
        async with session_factory() as session:
            edges = await link_narrative_arcs(session, story_id)
        result["arcs_created"] = sum(1 for e in edges if e.get("created"))
    except Exception as exc:
        result["errors"].append(f"narrative arcs: {type(exc).__name__}: {exc}")
        logger.warning("narrative arcs failed for %s: %s", story_id, exc)

    if dry_run:
        result["skipped"] = "dry_run"
        result["skip_detail"] = "no LLM call made"
        return result

    try:
        async with session_factory() as session:
            digest, unit_count = await claims_stage.story_evidence_snapshot(
                session, story_id, max_units=units
            )
            current = bool(digest) and await claims_stage.claims_are_current(
                session, story_id, digest
            )
        if current:
            result["skipped"] = SKIP_ALREADY_ANALYSED
            result["skip_detail"] = "claim matrix matches current evidence"
            return result
        if unit_count < claims_stage.MIN_CLAIM_UNITS:
            # One source is not corroboration. Skipped with its reason so the run
            # record distinguishes "nothing to read" from "the reading failed".
            result["skipped"] = SKIP_TOO_FEW_UNITS
            result["skip_detail"] = (
                f"{unit_count} unit(s) with article text, "
                f"{claims_stage.MIN_CLAIM_UNITS} needed"
            )
            return result
        extraction = await _extract_claims(
            session_factory, story_id, budget=budget, max_units=units, retries=retries
        )
        result["claims_created"] = extraction.get("claims_created", 0)
        result["evidence_created"] = extraction.get("evidence_created", 0)
        result["errors"].extend(extraction.get("errors") or [])
    except _Skip as exc:
        result["skipped"] = exc.reason
        result["skip_detail"] = exc.detail
    except Exception as exc:
        result["skipped"] = SKIP_ERROR
        result["skip_detail"] = f"{type(exc).__name__}: {exc}"
        result["errors"].append(str(exc))

    return result


async def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours-back", type=int, default=settings.phase2_hours_back,
        help="how far back to look for gate-passed stories (default %(default)s)",
    )
    parser.add_argument(
        "--max-stories", type=int, default=settings.phase2_max_stories_per_run,
        help="hard ceiling on stories per run (default %(default)s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run the no-cost stages and report what would be spent; make no LLM call",
    )
    args = parser.parse_args(argv)

    session_factory = get_session_maker()
    budget = Phase2TokenBudget()

    async with session_factory() as session:
        stories = await select_phase2_stories(
            session, hours_back=args.hours_back, max_stories=args.max_stories
        )
    print(
        f"Phase 2: {len(stories)} gate-passed stories in the last {args.hours_back}h "
        f"(cap {budget.cap} tokens/day, "
        f"{'dry run' if args.dry_run else f'{await budget.remaining()} tokens remaining'})"
    )
    if not stories:
        print("PHASE2_SUMMARY=" + json.dumps(summarize([])))
        return 0

    results = []
    for story in stories:
        record = await run_story(
            session_factory, story.id,
            budget=budget,
            units=settings.phase2_units_per_story,
            retries=settings.phase2_max_rate_limit_retries,
            dry_run=args.dry_run,
        )
        results.append(record)
        if record["skipped"]:
            print(
                f"  {record['story_id']}: skipped={record['skipped']} "
                f"({record['skip_detail']})"
            )
        else:
            print(
                f"  {record['story_id']}: topics={record['topics_created']} "
                f"arcs={record['arcs_created']} claims={record['claims_created']} "
                f"evidence={record['evidence_created']}"
                + (f" errors={record['errors']}" if record["errors"] else "")
            )

    summary = summarize(results)
    summary["tokens_spent_today"] = await budget.spent_today()
    summary["token_cap"] = budget.cap

    processed = summary["processed"]
    auth_failures = sum(
        1 for r in results
        if any(_is_auth_error(e) for e in r["errors"]) and not r["skipped"]
    )
    summary["llm_auth_failures"] = auth_failures

    summary_line = f"PHASE2_SUMMARY={json.dumps(summary, sort_keys=True)}"
    print(summary_line)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(summary_line + "\n")

    if processed and auth_failures > processed / 2:
        print(f"ERROR phase2_llm_auth_failed={auth_failures}/{processed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
