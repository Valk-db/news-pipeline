"""Phase 2 on gate-passed PENDING stories: selection, budget, and the run record.

## Why this module exists

The three enrichment stages -- topic groups (`topics.py`), narrative arcs
(`narrative.py`) and claim extraction (`claims.py`) -- each filtered
`Story.status == QUEUED`. Since the curation redesign nothing writes QUEUED, so
each job logged "no stories" and exited without doing anything. Measured on dev
2026-10-03: `PENDING 643, BLOCKED 1635`, and `claims 0 / claim_evidence 0 /
entity_edges 0` -- every one of those rows is the shape of a stage that has never
run, not of a stage that ran and found nothing.

The fix is to run them on gate-passed PENDING stories, and that is a smaller
change than it sounds because of two things this module is careful about.

## What Phase 2 is NOT

**Nothing here changes what is public.** `Story.Status.QUEUED` means "approved,
ready to post" and the public map and story pages filter on
`(QUEUED, POSTED)` (`curation_ui/discovery.py: PUBLIC_STORY_STATUSES`). PENDING
is the triage queue. Phase 2 attaches analysis to stories in the triage queue and
changes no status, writes no `curated_posts` row, and is reachable only from an
authenticated curation surface. A PENDING story id still 404s on every public
route after this runs, and `tests/test_phase2_isolation.py` is the test that says
so.

**$0 means no paid provider, not zero requests.** A cap of 0 would refuse every
request and turn Phase 2 off entirely. The budget here is a slice of the free
tier's real daily allowance, counted in the database so it holds across
processes, and exhausting it skips stories and records the skip rather than
failing the run.

## Scope guards

* **Viewpoint children are excluded.** A child story is one view of an event its
  parent already covers, so extracting claims for both spends the budget twice on
  the same evidence and produces two claim matrices that mostly agree. Only the
  parent is analysed. (Measured on dev 2026-10-03: 0 of 643 PENDING stories are
  viewpoint children, so this guard currently excludes nothing there -- it is
  insurance for the day viewpoint clustering actually runs, and it is tested
  against rows that do exist rather than against the empty set.)
* **Already-analysed stories are skipped** through the recomputable-derived-state
  mechanism (`src/shared/analyzer_versions.py`), before any spend. See
  `claims.claims_are_current`, which re-derives each stored claim's `input_hash`
  from data already in the row. A daily job that re-reads the same story every
  day is the single most expensive way to get nothing.

## Budget arithmetic, with the measurements behind it

Groq free plan, `openai/gpt-oss-20b`, published at
https://console.groq.com/docs/rate-limits and re-read on 2026-10-03:
**RPM 30, RPD 1,000, TPM 8,000, TPD 200,000.** The TPM/TPD figures were confirmed
against the live provider, not only the docs: a run of three claim extractions
tripped a real 429 whose body read "Rate limit reached ... on tokens per minute
(TPM): Limit 8000, Used 5011, Requested 3954."

Measured cost of one claim extraction on real dev stories:

| units in prompt | total tokens | prompt | completion |
|---|---|---|---|
| 3 | 1,742 | 1,171 | 571 |
| 5 | 2,437 | 1,790 | 647 |
| 10 | 4,962 | 3,462 | 1,500 |

So the limiter that actually binds is tokens, not requests: at 10 units a single
call is 62% of a minute's allowance, and 40 such calls exhaust the day's 200,000
tokens while a 1,000-request cap would still read 96% unspent. Hence a
token-denominated counter (`budget.GROQ_PHASE2_TOKENS`) and a pacing floor
between calls.

Measured current daily usage of the *other* counters on dev (2026-10-02, the last
day any Groq spend was recorded): `groq_requests 20`, `groq_translation_requests
68`. Those count requests, not tokens, which is itself the finding -- there is no
token-denominated counter for caption/classification work, so their daily token
cost cannot be read from the database and is inferred from their per-call
`max_tokens`. The default cap of 40,000 tokens/day (20% of the published daily
allowance) is sized so that Phase 2 cannot consume the tier even if every other
stage went quiet: 40,000 / 2,437 is ~16 stories a day, which covers the daily
inflow of gate-passed stories and works the PENDING backlog down at that rate.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import select

from src.schema.models import Story
from src.shared.budget import GROQ_PHASE2_TOKENS, spend, used
from src.shared.config import get_settings

logger = logging.getLogger(__name__)

# The statuses Phase 2 runs on, and why each is here.
#
# PENDING is the gate-passed state: src/verification/tiers.py writes PENDING when
# the corroboration gate passes and BLOCKED when it fails, so PENDING is the
# curation redesign's name for "gate-passed, awaiting a human". BLOCKED failed the
# gate and gets no LLM spend, which is the entire point of the gate.
#
# QUEUED is kept for the stories that were approved under the old flow and are
# still in the table. It is a subset of gate-passed by definition, so including
# it cannot widen exposure, and leaving it out would silently stop analysing
# anything a curator had already looked at.
PHASE2_STATUSES: tuple[Story.Status, ...] = (Story.Status.PENDING, Story.Status.QUEUED)

# Names used in the run record and the logs. Kept as constants so a log line and
# a test assertion cannot disagree about what a skip was called.
SKIP_ALREADY_ANALYSED = "already_analysed"
SKIP_BUDGET = "budget_exhausted"
SKIP_RATE_LIMITED = "rate_limited"
SKIP_OUT_OF_CREDIT = "out_of_credit"
SKIP_VIEWPOINT_CHILD = "viewpoint_child"
SKIP_ERROR = "error"


class Phase2BudgetRefused(Exception):
    """The day's Phase 2 token allowance is spent, or could not be read.

    A refusal is a normal outcome, not a failure: the caller skips the story and
    records the skip. Raised instead of returning a sentinel so that a caller
    cannot accidentally treat "I could not check" as "I have budget".
    """


async def select_phase2_stories(
    session,
    *,
    hours_back: int,
    max_stories: int,
    statuses: Sequence[Story.Status] = PHASE2_STATUSES,
    exclude_viewpoint_children: bool = True,
    now: datetime | None = None,
) -> list[Story]:
    """The gate-passed stories Phase 2 will consider, newest first.

    Newest first is a deliberate ordering, not a default: the exit criteria this
    batch is building towards depend on harm flags landing while the story is
    still being discussed, so a backlog of older PENDING stories must never
    starve today's. With a token cap of ~16 stories a day, "newest first" is the
    difference between timely detection and a queue that only ever reports on
    last week.

    Viewpoint children are excluded here rather than at the call sites, so all
    three stages share one definition and cannot drift apart.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours_back)
    stmt = (
        select(Story)
        .where(Story.status.in_(statuses))
        .where(Story.created_at >= cutoff)
        .order_by(Story.created_at.desc())
        .limit(max_stories)
    )
    if exclude_viewpoint_children:
        # A viewpoint child carries the PARENT's id in viewpoint_cluster_id
        # (src/verification/stories.py:325). Null means "not a child", which is
        # every ordinary story.
        stmt = stmt.where(Story.viewpoint_cluster_id.is_(None))
    return list((await session.execute(stmt)).scalars().all())


def estimate_prompt_tokens(prompt: str) -> int:
    """A deliberately pessimistic token estimate for a prompt.

    No tokenizer is a dependency of this project, so this is characters/2 rather
    than characters/4: English averages ~4 characters per token, so dividing by 2
    over-estimates, which is the direction that matters when the estimate gates a
    spend. An estimate that could under-count would let a call through the cap and
    then be discovered by the provider's 429 instead of by us.
    """
    return len(prompt) // 2 + 1


class Phase2TokenBudget:
    """The day's Phase 2 token allowance, enforced through the shared counter.

    Two steps per LLM call, and the order matters:

    1. `ensure_headroom(prompt)` reads the counter and refuses if the estimate
       would pass the cap. This is the gate.
    2. `record(usage)` spends what the provider actually reported, so the counter
       tracks real spend rather than an estimate that is either too low (the cap
       stops meaning anything) or too high (the cap starves the job at half the
       budget it was granted).

    The reservation is therefore *not* pre-committed, which is an honest trade and
    not an oversight: a run that reserves its worst case first would need a
    compensating decrement when the real usage came in lower, and this project
    has no refund primitive in `src/shared/budget.py`. The consequence is bounded
    and stated rather than hidden -- two concurrent Phase 2 runs could each pass
    the gate on the same last request's worth of headroom, so the cap is set at
    20% of the daily allowance and the overshoot is capped at one call. Phase 2
    runs from a single scheduled job with `concurrency` set, so this is a
    theoretical race rather than an observed one, and the daily cap is far enough
    below the provider's own limit that crossing it would need a real
    misconfiguration.
    """

    def __init__(self, cap: int | None = None):
        settings = get_settings()
        self.cap = cap if cap is not None else settings.phase2_daily_token_cap
        self._last_call_at: float | None = None

    async def spent_today(self) -> int:
        """Tokens charged to Phase 2 today, or -1 when the counter is unreadable."""
        value = await used(GROQ_PHASE2_TOKENS)
        return -1 if value is None else value

    async def remaining(self) -> int:
        spent = await self.spent_today()
        # An unreadable counter is treated as fully spent, which is the direction
        # src/shared/budget.py commits to everywhere else: a budget that cannot be
        # verified must not become an unlimited one.
        return 0 if spent < 0 else max(0, self.cap - spent)

    async def ensure_headroom(self, prompt: str) -> int:
        """Raise Phase2BudgetRefused unless `prompt` fits in what is left today."""
        estimate = estimate_prompt_tokens(prompt)
        remaining = await self.remaining()
        if estimate > remaining:
            raise Phase2BudgetRefused(
                f"estimated {estimate} tokens exceeds {remaining} remaining "
                f"of the {self.cap}/day Phase 2 allowance"
            )
        return estimate

    async def record(self, total_tokens: int) -> int:
        """Charge `total_tokens` against the day's allowance.

        The provider's own `usage.total_tokens`, so reasoning tokens are counted --
        they are real tokens against a real allowance, and the whole reason the
        counter is denominated in tokens rather than requests. A zero-token or
        absent usage is charged 1 rather than 0: the request happened, and a
        counter that cannot see a call is a counter that stops being a cap.
        """
        amount = max(1, int(total_tokens or 0))
        await spend(GROQ_PHASE2_TOKENS, amount, self.cap)
        return amount

    async def pace(self, sleeper=None) -> float:
        """Wait out the configured minimum gap since the previous call.

        Returns the seconds actually slept. Token-per-minute, not
        requests-per-minute, is the limit this exists for: at the measured 2,437
        tokens per call against a published TPM of 8,000, three calls a minute is
        the ceiling, so the default 25s floor allows 2.4. A first call does not
        wait -- there is nothing to be paced against.
        """
        gap = get_settings().phase2_min_seconds_between_calls
        if gap <= 0 or self._last_call_at is None:
            self._last_call_at = time.monotonic()
            return 0.0
        waited = gap - (time.monotonic() - self._last_call_at)
        if waited > 0:
            if sleeper is None:
                await asyncio.sleep(waited)
            else:
                sleeper(waited)
        self._last_call_at = time.monotonic()
        return max(0.0, waited)


def summarize(results: Iterable[dict]) -> dict:
    """Fold per-story result dicts into the run summary.

    Every skip reason is counted by name, because "the run processed 0 stories" is
    the single most useless thing a scheduled job can log and this batch exists
    because three jobs logged exactly that for months without anyone noticing.
    """
    results = list(results)
    skipped: dict[str, int] = {}
    counts = {
        "stories": 0,
        "topics_created": 0,
        "arcs_created": 0,
        "claims_created": 0,
        "evidence_created": 0,
        "errors": 0,
    }
    for result in results:
        counts["stories"] += 1
        for key in ("topics_created", "arcs_created", "claims_created", "evidence_created"):
            counts[key] += int(result.get(key, 0) or 0)
        counts["errors"] += len(result.get("errors", []) or [])
        reason = result.get("skipped")
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
    counts["skipped"] = skipped
    counts["skipped_total"] = sum(skipped.values())
    counts["processed"] = counts["stories"] - counts["skipped_total"]
    return counts


def new_result(story_id: uuid.UUID | str) -> dict:
    """One story's row in the run record, before anything has happened to it."""
    return {
        "story_id": str(story_id),
        "topics_created": 0,
        "arcs_created": 0,
        "claims_created": 0,
        "evidence_created": 0,
        "skipped": None,
        "skip_detail": None,
        "errors": [],
    }