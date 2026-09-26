# AGENT_TASKS.md v21

Supersedes v20. **P3-A and P3-B are done and merged** — commits `b66d923`
and `466b5ce` on `main`. Independently verified against a fresh clone:

- `ruff check .` — clean on `main`.
- `SourceTopicReliability` model present in `src/schema/models.py`;
  migration `20260927000000_source_topic_reliability.sql` present.
- `src/verification/reliability.py::compute_source_topic_reliability()` and
  its `MIN_SAMPLE_SIZE = 3` floor match the P3-B spec exactly.
- `scripts/compute_reliability.py` wired into `weekly-enrichment.yml`.
- Full suite on `main`: **358 passed, 5 skipped, 363 collected** — not the
  285 the summary claimed. Same class of self-reported-number error as the
  "277" one two rounds ago; worth building the habit of running `pytest -q`
  yourself before writing a number in a completion summary.

**P3-C is not merged, and "Phase 3 — Complete" is inaccurate.** Checked
`git branch -a --contains 1a7616a`: only `origin/p3-c-dynamic-gate`, not
`main`. `dynamic_gate_enabled` doesn't exist in `main`'s `config.py` either
— confirms it. Two of three Phase 3 tasks are done; the third is a pushed
branch, not a landed one.

## Why it wasn't merged, and why that's correct

`tests/test_dynamic_gate.py` on that branch: **7 failed, 3 passed** (not
"mock setup needs refinement" — 70% of the new file fails outright). Per
the standing merge policy (checks green → merge yourself), not merging a
red branch was the right call — the problem is the "Phase 3 — Complete"
framing sitting above that, not the branch being held back.

Sampled 3 of the 7 tracebacks to find the actual root cause before
prescribing a fix:

- `test_compute_admission_score_basic` — `StopIteration` inside
  `compute_harm_level`'s second `session.execute()` call. The mock's
  `side_effect` list has fewer entries than the number of `execute()` calls
  `compute_harm_level` actually makes.
- `test_apply_dynamic_gate_shadow_mode` — `AttributeError: 'tuple' object
  has no attribute 'id'` inside the *pre-existing*
  `recompute_story_counters` (unchanged by P3-C). The mock's return value
  for that call is shaped as raw tuples where `.scalars().all()` is
  expected to yield `Story` objects.
- `test_compute_virality_signal_no_viral` — `TypeError: object MagicMock
  can't be used in 'await' expression`. That particular call in the
  sequence got a plain `MagicMock()` instead of an `AsyncMock()`/awaitable
  result.

All three are **mock-configuration bugs in the test file, not logic bugs in
`tiers.py`** — nothing here indicates `compute_harm_level`,
`compute_admission_score`, or `apply_dynamic_gate` themselves are wrong,
only that the tests don't feed their mocked session enough correctly-shaped
`execute()` results to reach that conclusion. This is the same
`AsyncMock(spec=AsyncSession)` + `session.execute.side_effect = [...]`
pattern `tests/test_reliability_scoring.py` (P3-B) already uses
successfully — copy that file's setup style, don't invent a new one.

---

## P3-C-fix — Make `test_dynamic_gate.py` actually pass

### Goal
Not "make the 7 red tests green" — make them **correctly** green. A test
that's mocked into passing without actually exercising the real query
sequence is worse than a red test.

### Method, per failing test
1. Read the real function under test in `src/verification/tiers.py` end to
   end first. Count exactly how many `await session.execute(...)` calls it
   makes, in order, and what each one's result is used for
   (`.scalar_one_or_none()` vs `.scalars().all()` vs raw rows).
2. Build `mock_session.execute.side_effect` as a list with exactly that
   many entries, each shaped to match what that specific call site expects
   — a bare `MagicMock()` when `.scalar_one_or_none()` is called on it, one
   whose `.scalars().all()` returns the right objects when that's called
   instead. Don't pad with extra generic mocks to silence `StopIteration` —
   an extra or missing entry means the test doesn't match the real call
   sequence, which defeats the point of the test.
3. For `test_apply_dynamic_gate_shadow_mode` specifically:
   `apply_dynamic_gate()` delegates to the existing `apply_tier1_gate()`,
   which calls `recompute_story_counters()` — that function's mocked
   `execute()` result needs `.scalars().all()` to yield `Story`-like objects
   (objects with a real `.id`), not tuples. Check
   `recompute_story_counters`'s own existing tests (if any exist elsewhere
   in the suite) for a mock shape to copy rather than guessing one.
4. After each fix, run that one test in isolation
   (`pytest tests/test_dynamic_gate.py::<name> -v`) before moving to the
   next — don't fix all 7 blind and run the file once at the end.

### Acceptance
`pytest tests/test_dynamic_gate.py -v` — 10 passed, 0 failed.
`pytest -q` on the full branch — no new failures beyond the 6 pre-existing
`test_ner.py` failures if `en_core_web_sm` isn't downloaded in your sandbox
(same known gap as every prior round; download it and confirm 0 failures if
you want the clean number). `ruff check .` — clean (already is).

### Then merge
Per the standing merge policy: once genuinely green, merge
`p3-c-dynamic-gate` into `main` yourself. Don't re-summarize Phase 3 as
"complete" until this PR is actually merged — say what's landed and what
isn't in the same summary, the way this file does above.

---

## Not in this batch (unchanged from v20's backlog)
Certification badge, evidence snapshotting, media forensics, Phase 1a new
source categories, Phase 4 globe expansion, and wiring `fact_check_article()`
into a real job — none started, none blocked by anything in this task.