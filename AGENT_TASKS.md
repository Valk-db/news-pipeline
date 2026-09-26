# AGENT_TASKS.md v17

Supersedes v16. v16's P0-F/P0-G globe fixes are implemented and verified per
that revision's own record — if not yet merged, apply those independently;
they're unrelated to this revision (globe rendering vs. ingestion infra).

This revision is Phase 1 of `GRAND_PLAN.md` (§1b/§1c): three infrastructure
pieces the next wave of sources (sensor feeds, gov docs, OSINT tier,
translation) will plug into. No new sources are added by these tasks — this
is the plumbing, not the water.

Do P1-A and P1-B in either order (independent). Do P1-C any time — it's
independent of both, just wires into `src/shared/llm.py`.

---

## P1-A — Uniform source adapter contract

### Goal

`src/ingestion/run.py` currently branches per source
(`if SourceTier.TIER1 in tiers: ... ingest_rss_feeds(...)`, a separate branch
for GDELT, a separate branch for Reddit — see `run_ingestion()`). Every future
Phase-1 source (sensor feeds, gov docs, OSINT) would mean another bespoke
branch. Replace this with one adapter interface every source implements, so
adding a source means writing one adapter class, not editing the orchestrator.

**This is a refactor, not a behavior change.** Existing dedup, tier gating,
GDELT's circuit breaker, and the tier1-critical-domain exit-code logic in
`run.py` must produce identical output to today.

### New file: `src/ingestion/adapter.py`

```python
"""Common contract every ingestion source implements.

Nothing in an adapter fetches on construction -- only when `fetch()` is
called by the orchestrator in run.py. This mirrors the source_registry.py
convention of declaring config without doing network I/O at import time.
"""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from src.schema.models import RawArticle


@dataclass
class SourceHealth:
    """Health snapshot returned by a source adapter's health_check().

    status is one of "ok" | "degraded" | "down". succeeded/failed/skipped
    hold per-domain or per-feed identifiers -- gdelt.py's existing health
    dict (succeeded/failed/skipped keys) maps directly onto this shape.
    """
    status: str
    detail: str = ""
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@runtime_checkable
class SourceAdapter(Protocol):
    """Every ingestion source (RSS, GDELT, Reddit, and future sensor/OSINT/
    gov-doc sources) implements this."""

    name: str  # e.g. "rss_tier1", "rss_tier2", "gdelt", "reddit_tier3"

    async def fetch(self) -> list[RawArticle]:
        """Fetch and return new articles. Must not raise on a single feed's
        failure inside the batch -- log/record it via IngestStats and
        continue; only raise for a total adapter failure (e.g. the adapter
        cannot reach the network at all)."""
        ...

    async def health_check(self) -> SourceHealth:
        """Cheap status check. RSS/GDELT-style adapters should reuse the
        per-domain result from the most recent fetch() rather than making a
        fresh network call here."""
        ...
```

### New directory: `src/ingestion/adapters/`

Wrap the existing modules — **do not rewrite their internals.** Each adapter
is a thin class that calls the existing function and reshapes the result.

- `src/ingestion/adapters/rss_adapter.py` — wraps `ingest_rss_feeds()` from
  `rss.py`. One adapter instance per tier (`RssAdapter(tier=SourceTier.TIER1)`,
  `RssAdapter(tier=SourceTier.TIER2)`), sourcing its feed list from
  `get_enabled_sources_by_tier()` in `source_registry.py` exactly as `run.py`
  does today. `health_check()`: derive ok/degraded from whether the last
  `fetch()` returned zero articles for any enabled tier-1 domain (this is the
  same check `main()` already does per-source after the run today — move it
  into the adapter instead of leaving it in `main()`).
- `src/ingestion/adapters/gdelt_adapter.py` — wraps `ingest_gdelt()` from
  `gdelt.py`. Its `(articles, health)` return already has
  `succeeded`/`failed`/`skipped` keys — map straight into `SourceHealth`.
  Preserve the existing `GDELT_TIER1_CRITICAL_DOMAINS` check; it currently
  lives in `run.py` computing `tier1_critical_down` — keep that computation in
  `run.py` after calling `health_check()`, don't bury it inside the adapter.
- `src/ingestion/adapters/reddit_adapter.py` — wraps `ingest_reddit()` from
  `reddit.py`.

### Changes to `src/ingestion/run.py`

Replace the three `if SourceTier.X in tiers:` branches (lines ~103-131 in the
current file) with: build a list of adapters for the requested tiers, call
`.fetch()` on each (gather or sequential — match current sequential ordering
to avoid changing GDELT's circuit-breaker timing), extend `all_articles`, and
collect `.health_check()` per adapter into
`results["phases"]["ingestion"]["adapter_health"]` (a dict keyed by adapter
`name`) instead of the current ad hoc `gdelt_health` special case. Keep
`results["phases"]["ingestion"]["rss"]`, `["gdelt"]`, `["reddit"]` count keys
as-is for backward compatibility with anything reading that JSON downstream
(the curation UI's `/healthz`, any dashboards).

### Acceptance

`uv run python -m src.ingestion.run --dry-run` before and after this change
should produce byte-identical `results["phases"]["ingestion"]` counts (`rss`,
`gdelt`, `reddit`, `total_fetched`, `total_new`, `tier1_critical_down`) against
the same feed state. Diff the dry-run JSON output before/after as the check —
if anything differs, the refactor changed behavior and that's a bug, not an
improvement.

---

## P1-B — `DATA_SOURCES.md` license-risk ledger

### Goal

Catch a noncommercial-use restriction on an existing or new source *before*
it's load-bearing for a paying customer, not after. Modeled on
`gods-eye-view`'s `DATA_SOURCES.md` — see `GRAND_PLAN.md` §1c for why this
matters given the "sell access later" plan.

### New file: `DATA_SOURCES.md` at repo root

One row per source. Columns:

```
| Source | Used for | License / terms | Commercial-use risk | Attribution required | Cache/rate policy |
```

`Commercial-use risk` is one of: `none` / `attribution-only` /
`noncommercial — flag` / `unclear — needs review`.

**Populate retroactively for every existing source**, not just new ones:

- Every domain in `TIER1_SOURCES`, `TIER2_SOURCES`, `TIER3_SOURCES`,
  `TIER4_SOURCES` in `src/ingestion/source_registry.py` — check each
  publisher's actual RSS/republication terms (BBC, Guardian, NPR, DW,
  France24, Al Jazeera, Euronews, PBS NewsHour, NYT, FT, Economist, Foreign
  Policy, Foreign Affairs, CSIS, WHO, LA Times, Chicago Tribune, Boston Globe,
  SFGate, and the Reddit subs in `reddit.py`) — don't assume any of them are
  commercial-clear, verify.
- GDELT (`gdelt.py`) — its terms are already known to permit commercial use
  with citation per `GRAND_PLAN.md` §1c; row it anyway so the ledger is
  complete, not just the risky ones.

Add a one-paragraph header linking to `GRAND_PLAN.md` §1c so the "why does
this file exist" context isn't lost.

### Acceptance

Every domain key across the four tier dicts in `source_registry.py` has
exactly one corresponding row. Any row marked `noncommercial — flag` or
`unclear — needs review` gets called out explicitly in the PR description —
don't let one slide through silently in a large diff.

---

## P1-C — Groq daily-request budget governor

### Goal

Groq's free tier is 1K requests/day (`README.md`'s Cost table). Nothing today
tracks spend against that cap — `src/shared/llm.py`'s `LLMClient` calls Groq
directly per request. As Phase 1/2 add more LLM-driven extraction (claims,
translation, viewpoint clustering), this needs a governor before it needs a
429 to notice the cap exists. Pattern: `gods-eye-view`'s TomTom tile budget,
sized under the provider's published cap, not at it.

### New file: `src/shared/llm_budget.py`

```python
"""Daily request budget + in-flight coalescing for LLMClient's Groq calls.

In-memory, per-process daily counter -- resets at UTC midnight. This is
correct for the current twice-daily GitHub Actions run (one process per
run, no concurrent processes sharing a Groq key). It is NOT a distributed
limiter: if this pipeline ever runs as more than one concurrent process
against the same Groq key, replace this with a DB-backed counter.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import hashlib


@dataclass
class BudgetStatus:
    used_today: int
    limit: int
    remaining: int
    exhausted: bool


class BudgetExhausted(Exception):
    def __init__(self, status: BudgetStatus):
        super().__init__(
            f"Groq daily budget exhausted: {status.used_today}/{status.limit}"
        )
        self.status = status


class RequestBudget:
    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._count = 0
        self._day = datetime.now(timezone.utc).date()
        self._inflight: dict[str, "asyncio.Future"] = {}

    def _roll_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day = today
            self._count = 0

    def status(self) -> BudgetStatus:
        self._roll_if_new_day()
        remaining = max(0, self.daily_limit - self._count)
        return BudgetStatus(
            used_today=self._count,
            limit=self.daily_limit,
            remaining=remaining,
            exhausted=remaining == 0,
        )

    @staticmethod
    def _coalesce_key(messages: list[dict], model: str) -> str:
        raw = repr(messages) + model
        return hashlib.sha256(raw.encode()).hexdigest()

    async def run(self, coro_factory, messages: list[dict], model: str):
        """Run coro_factory() under the budget + coalescing. coro_factory is
        a zero-arg callable returning the awaitable Groq call, so an
        identical in-flight call is awaited a second time instead of
        dispatched twice. Raises BudgetExhausted if the daily cap is spent."""
        self._roll_if_new_day()
        key = self._coalesce_key(messages, model)
        if key in self._inflight:
            return await self._inflight[key]

        status = self.status()
        if status.exhausted:
            raise BudgetExhausted(status)

        fut = asyncio.ensure_future(coro_factory())
        self._inflight[key] = fut
        try:
            result = await fut
            self._count += 1
            return result
        finally:
            self._inflight.pop(key, None)
```

### Changes to `src/shared/config.py`

Add `groq_daily_request_budget: int = 900` to `Settings` — deliberately under
the published 1K cap to leave headroom, same reasoning as
`TOMTOM_DAILY_TILE_BUDGET` in `gods-eye-view` sizing under, not at, the
provider's real limit.

### Changes to `src/shared/llm.py`

- Give `LLMClient` a `RequestBudget` instance (constructed from
  `settings.groq_daily_request_budget` in `_init_clients()` or `__init__`).
- Route `_chat_completion_groq` through `self._budget.run(...)` instead of
  calling `self.groq_client` directly.
- On `BudgetExhausted`: fall back to Cerebras if `self.has_cerebras` (or the
  equivalent check `LLMClient` already uses for its Cerebras fallback path —
  reuse the existing fallback branch rather than adding a second one). If
  Cerebras isn't configured either, the caller needs a defined behavior:
  `generate_caption()` and `classify_relevance()` currently assume a result
  always comes back. Recommend: return `None` and have the ingestion run skip
  enrichment for that item rather than crash the run, recording it via
  `IngestStats.record("groq", "budget_skipped")` (`src/utils/ingest_stats.py`)
  so it surfaces in the existing run-summary markdown table instead of
  silently vanishing.

### Acceptance

Unit test in `tests/` (new file, e.g. `tests/test_llm_budget.py`):

1. Drive `RequestBudget` past `daily_limit` and assert `BudgetExhausted`
   fires on the next `.run()` call.
2. Fire two identical concurrent `.run()` calls (same messages + model) and
   assert the underlying `coro_factory` was invoked exactly once (coalescing
   works) — both callers still get the same result.
3. Assert the counter resets after mocking the clock across a UTC day
   boundary.

Existing LLM-dependent tests (`tests/test_enrichment.py`,
`tests/test_enrichment_pipeline.py`, etc.) must keep passing unchanged — wire
the budget in at whatever default limit doesn't make those tests trip it.

---

## Order of work

1. P1-A and P1-B can run in parallel (no shared files).
2. P1-C touches `llm.py`/`config.py` only — no overlap with A or B.
3. Run the full `pytest` suite after each task, not just at the end — these
   are meant to land as three separate, independently-revertable PRs.