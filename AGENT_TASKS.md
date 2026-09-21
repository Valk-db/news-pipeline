# AGENT_TASKS.md: news-pipeline task file (v7, updated 2026-09-21)

This file is the only task file in force. Ignore any older task files and anything you remember from earlier rounds, including earlier reports that work was "complete". Read it fully before doing anything.

Repo: Valk-db/news-pipeline. Python 3.12, uv, async SQLAlchemy, Supabase Postgres, GitHub Actions, FastAPI curation UI on Vercel.

## Non-interactive shell rules (read first)
- Before anything else set these for your shell session.
  PowerShell: `$env:GIT_PAGER="cat"; $env:GIT_EDITOR="true"; $env:GIT_TERMINAL_PROMPT="0"`
  bash: `export GIT_PAGER=cat GIT_EDITOR=true GIT_TERMINAL_PROMPT=0`
- Use `git --no-pager` for log/diff/show/grep/stash show. Use `git merge --no-edit`. Commit only with `git commit -m "subject" -m "body"`. Never run `git commit` without -m, `git rebase -i`, `git add -p`, `less`, `vim`, `nano`, or anything that waits for keyboard input.
- Never use commands that block until finished (for example `gh run watch`). Use one-shot commands such as `gh pr checks` and report "pending" if it is not done.
- Make targeted edits only. Never rewrite a whole script in one tool call, never print whole files to the terminal, read only the function you are editing.
- If a command shows no output for about 2 minutes, cancel it and report which command. Do not retry the same command more than twice. If you are stuck, stop and say which step you are on and what you tried.

## Working agreement
1. Work on a NEW branch created from the latest `origin/main` (names are given per task). Never push to main, never force-push, never merge a pull request yourself. main receives bot commits ("chore: heartbeat"), so always `git fetch origin` first.
2. Modify ONLY the files a task names. No features, endpoints, settings, DB schema changes or dependency changes. Do not touch curation_ui/ or anything under src/ this round.
3. Each commit message = a short subject plus a body paragraph explaining WHY, describing only what the diff contains. Never mention work that is not in the diff.
4. Tests run with DATABASE_URL UNSET: `uv run pytest tests/ -q`. Never set DATABASE_URL to the Supabase URL. Never print it, and never repeat anything that looks like a connection string or API key in your report.
5. Nothing runs against the production database from your machine. Do not run scripts/story_audit.py (except `--help`), scripts/init_db.py, cleanup_stale.py, enable_rls.py, seed_*.py, or `python -m src.ingestion.run` without `--dry-run`.
6. Do not weaken, delete or skip existing tests except where a task says so. Never commit .env.
7. Lint: ~220 pre-existing ruff findings. Do not run `ruff --fix` repo-wide; add no new findings in files you touch.
8. Do NOT claim a pull request exists unless you created it. A github.com/.../pull/new/... link is only the compare page.
9. EVIDENCE RULE. A step is "done" only when its VERIFY commands were run and passed. In every report paste the last lines of each VERIFY output. If a VERIFY fails, say so and stop. Never write "complete" from memory. GitHub Actions (Ubuntu) is the source of truth for tests: passing on this Windows PC does not prove CI is green. If `gh` is not installed, write "CI status unknown, Tyler must check the Actions tab".
10. Do the steps in order without asking me between steps, but stop at every CHECKPOINT.

## Verified state (main = 41bd39a, reviewed on Linux with the Actions history)
- origin/main is the only branch. `fix/audit-and-hygiene` was deleted after PR #4 was merged.
- CI AND DAILY INGESTION ARE BROKEN BY PR #4. All three workflows (ci.yml, daily-ingest.yml, cleanup.yml) contain `cache: 'uv'` under `actions/setup-python@v5`. That action only supports pip, pipenv and poetry and throws "Caching for 'uv' is not supported", so the job fails at the "Set up Python" step and nothing after it runs. Evidence: the scheduled Daily News Ingestion run on 24eee65 failed at step 3 "Set up Python" (the heartbeat step still committed because it is `if: always()`, so a bot commit does NOT prove ingestion ran); CI on 41bd39a failed at "Set up Python". Last successful ingestion: 11:27 UTC on 828fd39. `astral-sh/setup-uv` with `enable-cache: true` already caches uv, so the setup-python cache line is not needed.
- Tests on Linux, DATABASE_URL unset: 204 passed, 1 failed, 5 skipped. The failure is `tests/test_story_audit.py::TestComputeJaccardHistogram::test_outside_48h_excluded`. Cause: the test builds unit times from its own `datetime.now()`, the function calls `datetime.now()` again microseconds later, so a unit exactly 72h old falls just outside the 3-day window. A coarser Windows clock probably hides this, which would explain the reported 205 passed.
- scripts/story_audit.py: Step A is done (repo-root import path, `prepare_database_url`, `get_settings`). Step B is mostly done (`bucket_for`, `now` parameter, deduplicated near-misses, no doubled label prefix) but there is no `hours_window` parameter (48h is hard-coded) and no `would_attach` stat. Steps C, D and E are NOT implemented: `compute_story_metrics`, `summarize_story_metrics`, `format_crosstab`, `format_per_day`, `build_report` and `fetch_audit_data` do not exist. `run_audit` prints sections as it goes and returns only a header (the source still contains the placeholder comment "similar to above but collected"), so main() prints the header again and $GITHUB_STEP_SUMMARY gets only a header. Remaining defects:
  - D4: "articles with no unit" joins on the representative article; must be `RawArticle.reporting_unit_id IS NULL`.
  - D5: section 2 groups by `Story.day` for the last N days and prints the enum object; must use `Story.created_at`, all time, and the enum `.value`.
  - D6: section 3 prints one row per story and compares the linked-unit COUNT with the stored tier-1 ARTICLE count, and ALL owners with the stored TIER-1 owner count, so it flags false mismatches. Stored values come from `apply_tier1_gate` in src/verification/tiers.py: `tier1_unit_count` = sum over linked units of `source_tiers["tier1"]`; `distinct_owners` = number of distinct keys across linked units' `tier1_owner_groups`.
  - D9: units are fetched only for the last `--days` (filtered on `ReportingUnit.day`), and candidates come from that same list, so units in the first 48h of the window are compared against a truncated pool and the histogram understates matches.
- tests/test_verification.py: the `db_session` guard skips correctly but its skip message contains the full `settings.database_url` (with password). With `-v` or `-rs` and a Supabase URL in .env, the password is printed.
- vercel.json contains `"pythonVersion": "3.12"`. That key is not documented by Vercel; the Python version is set by `requires-python` in pyproject.toml (already `==3.12.*`) and 3.12 is Vercel's default.
- Grouping facts (from reading the code, for Task 3): `extract_entities(top_n=3)` keeps the top 3 PER LABEL and `build_stories` uses PERSON, ORG and GPE, so a unit has up to 9 entities (README says top-3 overall); `settings.top_n_entities` is read in `build_stories` and never used. A story's entity set is the union of its units', stored as `list(set)[:10]`, which drops entities in arbitrary, per-process order. With Jaccard >= 0.4: two 7-entity sets need 4 shared, two 9-entity sets need 6 shared, and a 3-entity unit can never attach to a story set of 8 or more (max 0.375). The fuzzy alias match in `EntityCanonicalizer.resolve` compares type-prefixed keys ("person:trump" vs "person:donald trump"), so short and long name forms only merge if the long form was seen first.

## Task R0: preflight
R0.1 Set the environment variables from "Non-interactive shell rules".
R0.2 `git --no-pager status --short` and `git --no-pager stash list`. Report the output as plain text. Do not edit any file yet. (An edited AGENT_TASKS.md is expected: Tyler replaced it with v7.)
R0.3 `git fetch origin`, then `git checkout -b fix/ci-restore origin/main`. The modified AGENT_TASKS.md carries over. If git refuses, run `git stash push -m v7-tasks`, checkout, then `git stash pop`. Make the first commit `git add AGENT_TASKS.md` then `git commit -m "docs: AGENT_TASKS.md v7" -m "Task file for the CI restore and audit completion rounds."` and push with `git push -u origin fix/ci-restore`.
R0.4 Baseline: `uv run pytest tests/ -q` with DATABASE_URL unset. Report the counts you see. On Linux expect 204 passed, 1 failed, 5 skipped; on Windows you may see 205 passed. Either is fine, continue.

## Task 1: restore CI and ingestion (branch fix/ci-restore). Push after every step.
### 1.1 Remove the unsupported cache line
- In `.github/workflows/ci.yml`, `.github/workflows/daily-ingest.yml` and `.github/workflows/cleanup.yml`, delete ONLY the line `          cache: 'uv'` under the `actions/setup-python@v5` step. Change nothing else. If Tyler already removed it on GitHub the grep below prints nothing; skip this step and say so.
- VERIFY: `git --no-pager grep -n "cache: 'uv'" -- .github` prints nothing. `git --no-pager diff --stat` shows exactly 3 workflow files with 1 deletion each.
- Commit subject: `ci: remove unsupported cache: 'uv' from setup-python`. Body: setup-python@v5 only supports pip, pipenv and poetry, so the step failed and blocked CI and ingestion; setup-uv already caches.

### 1.2 Make the histogram tests independent of the wall clock
- In tests/test_story_audit.py find every test that calls `compute_jaccard_histogram` (`git --no-pager grep -n "compute_jaccard_histogram" -- tests/test_story_audit.py`). In each one use a fixed constant `now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)`, build every unit `created_at` as `now - timedelta(...)`, and pass `now=now` to the function.
- In `test_outside_48h_excluded` change the old unit from 72 hours to 60 hours (inside the 3-day window, outside the 48h comparison window) and update the comment. The expected result stays `{0.0: 2}` and `units_with_no_candidates == 2`.
- Leave tests that only call `build_unit_entity_sets` or other pure helpers alone.
- VERIFY: `uv run pytest tests/test_story_audit.py -q` passes, run three times in a row. `git --no-pager grep -n "datetime.now" -- tests/test_story_audit.py`: list any remaining hits in your report and confirm none is in a test that calls `compute_jaccard_histogram`.
- Commit subject: `test(audit): make histogram tests independent of the wall clock`.

### 1.3 Stop the skip message from printing the database URL
- In tests/test_verification.py, `db_session` fixture, replace the skip message with the fixed text `Refusing to run integration tests: DATABASE_URL is not a localhost database`. Do not interpolate the URL or any part of it. Change nothing else.
- VERIFY: `uv run pytest tests/test_verification.py -q` passes with the 5 integration tests skipped (DATABASE_URL unset). `git --no-pager diff` shows only the message line changed.
- Commit subject: `test: do not print DATABASE_URL in the integration-test skip message`.

### 1.4 Remove the undocumented vercel.json key
- In vercel.json delete the `"pythonVersion": "3.12"` line and the comma before it so the JSON stays valid. Keep the `functions` block.
- VERIFY: `uv run python -c "import json; print(json.load(open('vercel.json')))"` prints only the functions key.
- Commit subject: `chore: remove undocumented pythonVersion key from vercel.json`. Body: the Python version is already pinned by requires-python in pyproject.toml.

### 1.5 Stop overlapping ingestion runs
- In `.github/workflows/daily-ingest.yml` add this top-level block directly after the `permissions:` block (do not touch the heartbeat step):
  ```
  concurrency:
    group: daily-ingest
    cancel-in-progress: false
  ```
- VERIFY: `git --no-pager diff` shows only those 3 added lines in that file.
- Commit subject: `ci: queue overlapping ingestion runs instead of running them together`. Body: two runs at once can collide on the unique url_hash index and lose the whole batch.

### 1.6 Push and open the pull request
- `git push origin fix/ci-restore`. If `gh` is installed: `gh pr create --base main --head fix/ci-restore --title "Restore CI and ingestion" --body "Removes unsupported cache: 'uv', fixes a clock-dependent test, stops the skip message printing DATABASE_URL, removes an undocumented vercel.json key, adds an ingestion concurrency group."` then wait about 2 minutes and run `gh pr checks fix/ci-restore` once. If `gh` is not installed, give me the compare URL and write "CI status unknown".
- In CI the Postgres service is localhost, so the 5 integration tests should RUN there. Expect about 210 passed and 0 skipped. If any test fails in CI, report the test names and the failing step and STOP; do not fix it this round.

## CHECKPOINT 1: STOP
Report: `git --no-pager log --oneline -8`, local test counts, the PR URL, the CI result (or "unknown"), and the pasted VERIFY output for each step. Tyler merges the PR himself. Do not start Task 2 until Tyler says "go 2".

## Task 2: finish the audit script (ONLY after Tyler says "go 2"; branch fix/audit-completion from the updated origin/main)
Fixed names, so partial progress stays compatible. Keep the DB layer thin (plain SELECTs). After each step: run `uv run pytest tests/test_story_audit.py -q`, commit, `git push -u origin fix/audit-completion`, then go straight to the next step. Edit only scripts/story_audit.py and tests/test_story_audit.py. Every test that calls `compute_jaccard_histogram` must pass an explicit `now`.

### Step B2: finish the histogram function (existing signature stays the contract)
- Add `hours_window: int = 48` to `compute_jaccard_histogram` and use it instead of the hard-coded `48 * 3600`. Add the stat `would_attach` = analysed units whose best Jaccard is >= 0.4. Keep every existing stat key (`total_recent_units`, `units_with_entities`, `units_with_no_entities`, `units_with_no_candidates`, `zero_overlap_pairs`) and the return shape `{"histogram", "near_misses", "stats"}`.
- Tests: `hours_window` is honored (a pair 30h apart is excluded with `hours_window=24` and included with the default); `would_attach` counts a best Jaccard of exactly 0.4 and not 0.39.
- Commit subject: `audit-fix B2: hours_window parameter and would_attach stat`

### Step C: story metrics (fixes D6, pure functions only, not wired in yet)
- C1 `compute_story_metrics(stories, story_units, unit_info) -> list[dict]`. Inputs: `stories` = list of dicts `{id, status, tier1_unit_count, distinct_owners}` (status as a plain string); `story_units` = dict story_id -> list of unit ids; `unit_info` = dict unit_id -> `{"source_tiers": {...}, "tier1_owner_groups": {...}}`. Per story return `{id, status, linked_units, computed_tier1_count, computed_distinct_owners, stored_tier1_count, stored_distinct_owners, mismatch}`. computed_tier1_count = sum of `source_tiers.get("tier1", 0)` over linked units; computed_distinct_owners = size of the union of `tier1_owner_groups` keys; `mismatch` = stored pair differs from computed pair.
- C2 `summarize_story_metrics(metrics, max_examples=10) -> dict` with `crosstab` ({(linked_units, computed_distinct_owners): {status: count}}), `statuses` (sorted list), `mismatch_count`, `examples` (at most max_examples mismatching rows).
- C3 Tests on hand-built inputs: a story with no links is (0, 0); the tier-1 count is the sum of `source_tiers["tier1"]`, not the number of linked units; a stale stored value is flagged; the crosstab counts by status.
- Commit subject: `audit-fix C: story metrics and stored-vs-computed`

### Step D: report assembly (fixes D3, pure functions only)
- D1 Add `format_crosstab(summary) -> str`, `format_per_day(rows, title) -> str` and `build_report(data, days, host, now=None) -> str` that returns the ENTIRE markdown. Headings, each exactly once: `# Story Audit Report (last N days)`, `## 1. Counts`, `## 2. Per-day activity (all time)`, `## 3. Story shape: linked units x tier-1 owners`, `## 4. Entity-set size per unit`, `## 5. Cross-owner Jaccard`, `## 6. Duplicate (source_domain, title)`. Include the approximation note (alias merging is not done, so overlap may be understated) and the host line. Document the `data` keys in the docstring: counts (dict), stories_per_day, articles_per_day, units_per_day, orphans_per_day (lists of `{day, status, count}`), story_summary, entity_sizes, jaccard (the dict from `compute_jaccard_histogram`), duplicates.
- D2 Section 2 shows stories created, articles fetched and units created per UTC day, plus stories with no linked units by created day and status. Section 4 shows min/median/max and the number of units with an EMPTY set. Section 5 shows the histogram (including 0.0), all stats including `would_attach`, and the near-miss table. Section 6 shows at most 25 rows.
- D3 Tests: each heading appears exactly once, the report header exactly once, an all-empty `data` renders without crashing.
- Commit subject: `audit-fix D: build_report single-report assembly`

### Step E: SELECT-only data layer and wiring (fixes D3, D4, D5, D6, D9)
- E1 `fetch_audit_data(session, days, now=None) -> dict` with SELECT statements only, producing exactly the keys documented in D1. Counts: raw_articles, reporting_units, story_unit_links, stories, stories with no linked units, units whose representative article is missing, articles with `reporting_unit_id IS NULL` (D4). Per-day tables use `func.date` on `Story.created_at`, `RawArticle.fetched_at`, `ReportingUnit.created_at`, all time, with status via the enum `.value` (D5). Section 3 data comes from all stories, all links and `unit_info` per D6. For sections 4 and 5 fetch units with `ReportingUnit.created_at >= now - (days*24 + 48) hours` so units near the start of the analysed window still see their full 48h candidate pool (D9); `compute_jaccard_histogram` still analyses only units created within `days`, and section 4 counts only those units.
- E2 `run_audit` = engine -> session -> `SET TRANSACTION READ ONLY` (first statement) -> `fetch_audit_data` -> rollback -> `return build_report(...)`. `main()` prints that string ONCE and appends the identical string to $GITHUB_STEP_SUMMARY when set. Remove all dead code (per-story dump, incremental printing, the placeholder comment). Keep: no init_db(), no EntityCanonicalizer, no commits, host only (never user or password).
- E3 Add a test `test_no_print_in_data_layer` that parses scripts/story_audit.py with `ast` and asserts no `print(...)` call inside `run_audit` or `fetch_audit_data`.
- E4 VERIFY, paste all output: `git --no-pager grep -c -E "def (bucket_for|compute_story_metrics|summarize_story_metrics|format_crosstab|format_per_day|build_report|fetch_audit_data)" -- scripts/story_audit.py` prints `scripts/story_audit.py:7`. `git --no-pager grep -n "similar to above" -- scripts` prints nothing. `uv run python scripts/story_audit.py --help` and `uv run python -m scripts.story_audit --help` both exit 0. `uv run pytest tests/ -q` with DATABASE_URL unset passes with the 5 integration tests skipped, and the CI result of the PR.
- Commit subject: `audit-fix E: SELECT-only data layer and single-report output`

## CHECKPOINT 2: STOP
Push and report: `git --no-pager log --oneline -8`, test counts, files changed, the E4 output, the compare URL or PR URL and CI result. Tyler will run the script himself against production.

## Task 3: propose grouping changes (ONLY after Tyler pastes the audit output and says "go 3")
Do not change code. For each option, use the audit numbers to estimate how many extra cross-owner unit pairs it would match, and show 5 example pairs it would newly match so Tyler can judge whether they are truly the same event:
- (i) keep only the top-3 entities overall by frequency, as the README says;
- (ii) overlap coefficient or a min-shared-count rule instead of Jaccard;
- (iii) compare against the story's most recent unit instead of the growing union;
- (iv) frequency-ordered truncation instead of `list(set)[:10]`;
- (v) a backfill script that rewrites existing url_hash values using canonicalize_url (dry-run by default, collision report first);
- (vi) drop entities that are the outlet's own name or its reporters (BBC, Guardian, NPR, DW, France 24 and bylines) before matching; check the "shared entities" column of the near-miss table to see whether this is real;
- (vii) make `EntityCanonicalizer.resolve` compare names without the type prefix and at token level.
Write the proposal in your reply, not in a file. Then STOP and wait for Tyler's choice.

## Decisions for Tyler (agent: do nothing about these)
- PR #4 added DW and France 24 as tier-1 feeds and raised PENDING expiry from 72h to 120h. The previous task file listed the AP/Reuters replacement as Tyler's decision and limited edits to two files. Tyler to confirm he wants both changes. Do not revert or extend them.
- Heartbeat: it runs `if: always()`, so a bot commit does not mean ingestion succeeded, and its `git push` can race with pushes to main.
- Feeds cover world, politics and US news only; no entertainment feeds exist, so the celebrity vertical cannot produce stories. OWNERSHIP_GROUPS lists Penske outlets that are never ingested.
- /healthz on the curation UI is unauthenticated (story status counts, DB error text).
- curation_ui/pyproject.toml and curation_ui/uv.lock duplicate the root project files.
- Rotate the Supabase database password if a pytest run with `-v` or `-rs` ever printed the skip message while .env pointed at Supabase.
- Off-limits until Tyler instructs: GDELT changes, embeddings/pgvector, the heartbeat mechanism, gate logic, grouping code, the curation UI, expiring or deleting orphan stories, editing production data.

## Test-compatibility notes
- tests/test_story_audit.py imports the script as `from story_audit import ...` after inserting the scripts/ directory into sys.path; keep that working. `python -m scripts.story_audit` must keep working too.
- Existing tests elsewhere patch by name `src.ingestion.run.ingest_rss_feeds / ingest_gdelt / ingest_reddit` and `src.ingestion.{rss,gdelt,reddit}.extract_article`; do not touch those modules.
- After `uv sync`, spaCy's en_core_web_sm may be missing locally: `uv run python -m spacy download en_core_web_sm`.

## Report format after every checkpoint
Files changed; tests run with pass/fail counts; the pasted VERIFY output; CI result or "unknown"; anything surprising; what you need from me.