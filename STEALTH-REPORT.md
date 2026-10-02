# STEALTH REPORT — explainable corroboration gate (`gate_decisions` + wire-copy collapsing)

Worktree: `/home/hatch/workspace/wt-gate-explain` · branch `procmon/batch-gate-explain`
Parent: `b1d3f0b` (procmon/phase-0-foundations) · **not pushed, not merged, no Vercel deploy**
Dev target only: Supabase ref `qzothzirwwpesafzlxtw` (via `pg-tunnel.py`, port 15433 — tunnel killed at end;
the 15432 tunnel belonging to another agent was left untouched). Production never contacted.

## Commits
- `5e56746` Explainable corroboration gate: append-only gate_decisions + wire-copy collapsing
- `a557930` Fix histogram fallback double-count; repair tests for the new query shape
- `387e2d2` Add gate_decisions tests: ledger rows, wire collapse, append-only SQL
- (this report lands in a final commit) Worktree clean at report time; 3 files still touched by the killed run were reconciled in place.

## Files added / changed
- `src/verification/corroboration.py` (new, 401 lines) — single home for wire collapsing: `load_corroboration()`,
  `effective_owner()`, `ArticleEvidence`/`Corroboration`, per-unit owner histogram, `tier1_pairs()`, `collapse_note()`.
- `src/schema/models.py` — killed run's `Story.gate_decision` column **removed**; new `GateDecision` table
  (15 cols incl. `owner_groups`/`contributing_articles`/`breakdown` jsonb, `Index(story_id, decided_at DESC)`).
- `supabase/migrations/20261002110000_gate_decisions.sql` (new) — idempotent `CREATE TABLE IF NOT EXISTS` + index + COMMENTs
  ("append-only: INSERT only"); replaces deleted `20261002100000_stories_gate_decision.sql`.
- `src/shared/analyzer_versions.py` — `GATE_VERSION = "gate/v1"`; `gate_decisions` in `DERIVED_TABLES` + `TABLE_VERSIONS`;
  new `NOT_RECOMPUTED_TABLES = ("gate_decisions",)` so `scripts/recompute_derived.py` can never delete ledger rows;
  `assert_all_covered()` unions all three sets.
- `src/verification/tiers.py` — `recompute_story_counters` now derives `distinct_owners` from corroboration (post-collapse);
  `record_gate_decision()` appends a row per evaluated story; `apply_tier1_gate` + `apply_dynamic_gate` (incl. shadow) both record.
- `tests/test_gate_decisions.py` (new, 10 tests) — ledger rows, evidence chain, wire collapse, append-only.
- `tests/test_dynamic_gate.py`, `tests/test_viewpoint_clustering.py`, `tests/test_recomputable_derived_state.py` — repaired for the new query shape / registry.

## Salvaged vs replaced from the killed run
- **Salvaged:** the payload shape idea (article/url/domain/tier/hash evidence), the migration idiom, the SQLite-via-`db_session`
  test fixtures, and the "gate writes exactly one decision object per evaluation" framing.
- **Replaced:** `Story.gate_decision` JSON column → append-only `gate_decisions` table (a column loses history on re-gate, which
  is the whole point of the ledger); `build_gate_decision()`/`tier1_owner_histogram()` → `corroboration.py`; its "wire" tests only
  resolved `OWNERSHIP_GROUPS` domains — **not** the spec's content_hash collapse, so they were replaced with real
  same-`content_hash` fixtures; its 505-line `tests/test_gate_decision.py` merged into `tests/test_gate_decisions.py` (never two
  overlapping files).

## Tests
- New: `tests/test_gate_decisions.py` — **10 passed** (row per evaluation for both gates; breakdown ↔ gate_reason agreement;
  1 AP + 3 same-hash republications → `distinct_owners == 1`, `wire_origin` on the 3 copies only; shadow mode records dynamic
  rows with `breakdown.shadow`; re-gating **appends**; SQL listener proves no `UPDATE`/`DELETE` against the table; input_hash
  is a digest of the evidence).
- Regression set (gate/tiers/stories/units + touched neighbours): `test_gate_decisions test_dynamic_gate test_tiers
  test_verification test_counter_mismatch test_viewpoint_clustering test_recomputable_derived_state` → **82 passed, 7 skipped**.
- Full suite (`pytest -q -p no:randomly`, `NO_PROXY`/`no_proxy` unset — this VM's literal `[::1]` breaks
  `httpx.AsyncClient`): **1198 passed, 20 skipped, 8 failed**. All 8 failures (5 `test_content_hash.py::TestRunIngestionDedup`,
  1 `test_gdelt.py::TestRunIngestionIntegration`, 2 `test_gdelt_toggle.py::TestGdeltToggle`) reproduce on a clean
  `git worktree add /tmp/opencode/parent-baseline b1d3f0b` parent tree → pre-existing, none in files touched here.
- `ruff check` clean on all 8 changed/added Python files.

## Live evidence (dev Supabase, 127.0.0.1:15433)
- Migration applied, then **re-applied with no error** (idempotent). Verified 15 columns with correct types/nullability/defaults
  (`owner_groups`/`contributing_articles`/`breakdown` = jsonb, `decided_at` default `now()`, `id` default `gen_random_uuid()`),
  indexes `gate_decisions_pkey` + `ix_gate_decisions_story_decided_at` (story_id, decided_at DESC), COMMENT present.
- Real gate run over **19 linked dev stories** via `apply_tier1_gate`: `{'queued': 1, 'blocked': 18}` → **19 rows appended,
  one per story**; 0 changes to any story's status, gate_reason, or distinct_owners (no behaviour drift on clean data); 19 rows
  with populated `contributing_articles`; **0 wire copies collapsed**.
- Scratch story (1 apnews.com + cnn/washingtonpost/npr sharing `content_hash`): stored histogram was pre-collapse
  `[{AP:1},{NPR:1},{WaPo:1},{Independent:1}]`; after: **BLOCKED**, `distinct_owners=1`, `tier1_unit_count=4`,
  gate_reason `Blocked: Tier-1 units from only 1 owner(s) (need ≥2 distinct) (3 wire copies collapsed to AP)`. The tier1 row
  stored `owner_groups {"AP": 4}`, `wire_origin=apnews.com` on exactly the 3 copies and null on AP's own article; the dynamic
  shadow row stored `score=75, threshold=50, breakdown{..., "shadow": true}`. `jsonb_pretty(contributing_articles)` read back
  from Postgres round-tripped cleanly. Re-gating appended a 3rd row whose `input_hash` equalled the first tier1 row's
  (recomputable digest proven on real Postgres); deleting the scratch story cascaded its decisions to 0.
- Cleanup: the demo's 12 scratch `raw_articles` + 12 `reporting_units` (not cascaded by the story delete) were removed after
  checking every FK into `raw_articles` (only `reporting_units` had rows). Dev left with 0 scratch rows and **76
  `gate_decisions` rows** — deliberately kept: the ledger is append-only and these are the live evidence. Dev is shared with
  other agents; a transient 1-row count seen during migration was another agent, not us.

## Wire-collapse limitation (important)
An article collapses only if its `content_hash` matches an article whose `source_domain` is a `SourceCategory.WIRE_SERVICE`
source in `src/ingestion/source_registry.py`. **Both wire sources (apnews.com, reuters.com) are `enabled=False`**, the last
RSS pull was 2026-09-21 and GDELT is disabled, so dev had **0 wire-origin content hashes and 0 republications** before my
scratch demo — 0 collapses in live data. The mechanism is live and unit-tested; it simply has no input yet. It is mechanical
only (`content_hash` + registry lookup, no byline NLP), and because collapsing can only *narrow* apparent independence it can
only ever block, never let a story through. The one intended behaviour change is this counting change — nothing else in gate
semantics moved, and `Story.gate_reason` is still written exactly as before (scripts + curation UI read it).

## Deferred
- Re-running the live gate on dev to populate the ledger once real wire ingestion re-enables; today's 76 rows came from one run.
- A backfill of historical decisions is not possible (there is no pre-existing state to explain); the ledger starts at first run.
- No index on the jsonb evidence columns: no query needs them yet, and the `(story_id, decided_at DESC)` index covers reads.
- `NOT_RECOMPUTED_TABLES` is the guard against a ledger wipe by `recompute_story_counters`/derived-stage drops; if a future stage
  ever needs to touch `gate_decisions`, that stage must be added deliberately.

## Decisions needed
1. **Score vs boolean disagreement.** `compute_admission_score`'s `corroboration` factor is `min(tier1_articles*10, 30)`, so it
   counts collapsed copies as if they were independent: the scratch story scored **75 ≥ 50 (pass)** while the boolean gate
   **blocked** it. No code change made (the spec forbids other gate-semantics changes). Options: feed the post-collapse
   distinct-owner count into the score's corroboration factor, or accept the split (score = ranking signal, boolean gate =
   admission) and document it. Also fixes: GATE_VERSION is currently `"gate/v1"` even though the wire change landed with it —
   bump to `gate/v2` (or keep and note pre-collapse rows) depending on the answer.
2. Whether the 76 live `gate_decisions` rows should stay in dev (they are the evidence above) or be cleared so dev starts clean.
3. Whether wire sources should be re-enabled / backfilled now, which is the only way collapsing affects production-shaped data.
