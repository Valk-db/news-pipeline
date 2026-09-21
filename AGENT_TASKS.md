# AGENT_TASKS.md: news-pipeline task file (v6, updated 2026-09-21)

This file is the only task file in force. Ignore any older task files and anything you remember from earlier rounds. Read it fully before doing anything. A previous session hung partway through Task 2R, so this version splits the work into small pushed steps and adds non-interactive shell rules.

Repo: Valk-db/news-pipeline. Python 3.12, uv, async SQLAlchemy, Supabase Postgres (+ pgvector), GitHub Actions, FastAPI curation UI on Vercel.

## Non-interactive shell rules (read first)
- Before anything else set these for your shell session.
  PowerShell: `$env:GIT_PAGER="cat"; $env:GIT_EDITOR="true"; $env:GIT_TERMINAL_PROMPT="0"`
  bash: `export GIT_PAGER=cat GIT_EDITOR=true GIT_TERMINAL_PROMPT=0`
- Use `git --no-pager` for log/diff/show/stash show. Use `git merge --no-edit`. Commit only with `git commit -m "subject" -m "body"`. Never run `git commit` without -m, `git rebase -i`, `git add -p`, `less`, `vim`, `nano`, or anything that waits for keyboard input.
- Make targeted edits only. Never rewrite scripts/story_audit.py in one tool call, never print whole files to the terminal, and read only the function you are editing.
- After each step run only `uv run pytest tests/test_story_audit.py -q`, then commit and PUSH. Run the full suite only at the end of Step E.
- If a command shows no output for about 2 minutes, cancel it and report which command. Do not retry the same command more than twice. If you are stuck, stop and say which step you are on and what you tried.

## Working agreement
1. Modify ONLY the files a task names (this round: scripts/story_audit.py and tests/test_story_audit.py). No features, endpoints, docs, settings, indexes, CLI flags other than `--days`, or DB schema changes. Do not touch curation_ui/ or src/verification/tiers.py.
2. Work on the EXISTING branch `fix/audit-and-hygiene`. Never merge into main, never push to main, never force-push.
3. Each commit message = a short subject plus a body paragraph explaining WHY, describing only what the diff contains. Use the subject prefixes given below (`audit-fix A:` etc.) so progress is visible in `git log`.
4. Tests run with DATABASE_URL UNSET (`uv run pytest tests/ -q`; the 5 Postgres integration tests skip locally). Never set DATABASE_URL to the Supabase URL: the integration fixtures TRUNCATE tables.
5. Nothing here runs against the production database from your machine. Do not run the audit script (except `--help`), scripts/init_db.py, cleanup_stale.py, enable_rls.py, seed_*.py, or `python -m src.ingestion.run` without `--dry-run`.
6. Do not weaken, delete, or skip existing tests outside tests/test_story_audit.py. Never commit .env or print secrets.
7. Lint: ~220 pre-existing ruff findings. Do not run `ruff --fix` repo-wide; add no new findings in files you touch.
8. Do NOT claim a pull request exists unless you created it. A github.com/.../pull/new/... link is only the compare page.
9. Do the steps in order without asking me between steps, but stop at every CHECKPOINT.

## Verified state
- origin/main = 48a432a (a bot heartbeat on top of 828fd39, which contains PRs #1 to #3). Ingestion works: ~2-minute runs, 218/218 RSS articles extracted, GDELT off, Reddit skipped, AP/Reuters removed. Earlier fixes are done and tested; do not redo them.
- origin/fix/audit-and-hygiene is still at f25e3d3 (two commits on top of 828fd39). Nothing was pushed after the hang; any local edits are unpushed and unreviewed. Task 1 (RSS retry tests patch asyncio.sleep; canonicalize_url docstring) is done and good: suite ~6 s, 205 passed / 5 skipped. Do not redo it.
- scripts/story_audit.py at f25e3d3 was reviewed and run against a synthetic Postgres with known answers. Defects (D1 to D8) that Steps A to E fix:
  - D1: `uv run python scripts/story_audit.py` fails with `No module named 'src'` (it adds `<repo>/src` to sys.path instead of the repo root; scripts/cleanup_stale.py does it right).
  - D2: it builds its own engine, bypassing `prepare_database_url` (src/shared/database.py): `?sslmode=` URLs raise TypeError and the Supabase transaction pooler will fail. It also reads only `os.environ["DATABASE_URL"]`, but Tyler's URL is in `.env` (read by `get_settings()`).
  - D3: `run_audit` prints sections, then returns only a header plus a placeholder; main() prints it again and writes it to $GITHUB_STEP_SUMMARY, so the summary has no sections.
  - D4: "articles with no unit" counts articles that are not a unit's representative; it must be `RawArticle.reporting_unit_id IS NULL` (synthetic data: printed 2, correct 1).
  - D5: "stories created per day" groups by `Story.day` for the last N days and prints `Status.BLOCKED`; it must use `Story.created_at`, all time, and the enum `.value`.
  - D6: section 3 dumps one row per story and compares the wrong definitions. Stored values come from `apply_tier1_gate` in src/verification/tiers.py: `tier1_unit_count` = sum over linked units of `source_tiers["tier1"]`; `distinct_owners` = number of distinct keys across linked units' `tier1_owner_groups`.
  - D7: bucketing uses `round(j*10)/10` (0.167 lands in 0.2, 0.39 in 0.4); `--days` is ignored (hard-coded 3 days and 48*3600; `hours_window` unused); zero-overlap and no-candidate units are silently dropped; each near-miss pair appears twice; shared entities show `PERSON:PERSON:...` because `_normalize_text` already returns a label-prefixed key.
  - D8: the tests are too weak (`assert len(near_misses) >= 0`, `assert 0.3 in histogram or 0.4 in histogram`).
  - Keep: the first statement is `SET TRANSACTION READ ONLY`; no init_db(); no EntityCanonicalizer; no commits; it prints the host only.

## Task R0: recovery and preflight
R0.1 Set the environment variables from "Non-interactive shell rules". If a pager or editor is open in your terminal, quit it first.
R0.2 `git --no-pager status --short` and `git --no-pager diff --stat`. Report what is modified or untracked as plain text. Do not edit any file yet.
R0.3 Preserve everything: `git stash push -u -m "wip-audit-hang"` (skip if there is nothing to stash), then `git branch wip-backup-audit` (a local backup of the current HEAD).
R0.4 `git fetch origin`, `git checkout fix/audit-and-hygiene`, `git reset --hard origin/fix/audit-and-hygiene` (safe now: the stash and backup branch hold your local work), then `git merge --no-edit origin/main` (only a heartbeat commit differs).
R0.5 Baseline: `uv run pytest tests/ -q` with DATABASE_URL unset. Expect 205 passed, 5 skipped. If not, STOP and tell me.
You may look at the stash (`git --no-pager stash show -p`) for ideas, but never apply it wholesale. Port a piece only if it matches the spec below.

## Task 1: DONE. Do not redo.

## Task 2R: fix the audit script in five pushed steps
Fixed names, so partial progress stays compatible. Keep the DB layer thin (plain SELECTs). After each step: run `uv run pytest tests/test_story_audit.py -q`, commit, `git push origin fix/audit-and-hygiene`, then go straight to the next step.

### Step A: entrypoint and connection (fixes D1, D2). Edit only the imports at the top, the engine/host lines in `run_audit`, and `main()`.
- A1 Replace the `sys.path.insert(... "src")` line with an insert of the repo root, as in scripts/cleanup_stale.py (`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`). `python -m scripts.story_audit` must keep working.
- A2 In `main()` parse args first (so `--help` needs no database), then `settings = get_settings()`. If `not settings.has_database`, print a clear message to stderr and exit 1. Use `settings.database_url`; delete the manual `postgresql://` replacement.
- A3 In `run_audit`: `url, connect_args = prepare_database_url(db_url)`; `engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args, echo=False)`. Print host and port from `url.host` / `url.port`, never username or password. Keep `SET TRANSACTION READ ONLY` as the first statement, end with `await session.rollback()` and `await engine.dispose()`.
- Verify: `uv run python scripts/story_audit.py --help` and `uv run python -m scripts.story_audit --help` both exit 0.
- Commit subject: `audit-fix A: entrypoint imports and Supabase-safe engine`

### Step B: pure histogram functions (fixes D7). Edit only the pure functions, their tests, and the single call site in `run_audit`.
- B1 `build_unit_entity_sets`: add `_normalize_text(surface, label)` directly (it already returns e.g. `PERSON:ron desantis`); do not add a second prefix.
- B2 Add `bucket_for(j: float) -> float` = `min(math.floor(j * 10 + 1e-9) / 10, 1.0)` (0.39 -> 0.3, 0.4 -> 0.4, 1.0 -> 1.0).
- B3 Change the signature to `compute_jaccard_histogram(units, unit_entities, unit_owner_groups, articles, days=3, hours_window=48, now=None) -> dict` returning `{"histogram": {bucket: count}, "near_misses": [...], "stats": {"analysed", "skipped_empty", "no_candidate", "would_attach"}}`. `now` defaults to the current UTC time and is injectable for tests. Analysed units = created_at >= now - days. Candidates = any unit in `units` within +/- hours_window with owner groups disjoint from the unit's and a non-empty entity set. Each analysed unit's best Jaccard goes through `bucket_for`, including 0.0 (units with candidates but no overlap). `skipped_empty` = analysed units with an empty entity set; `no_candidate` = units with no eligible candidate; `would_attach` = units whose best J >= 0.4. Near-misses: unordered pairs (dedupe on frozenset of the two ids, keep the highest J), only J in [0.2, 0.4), top 25 descending.
- B4 Update the one call site in `run_audit` minimally so the script still runs: unpack the dict and pass `res["histogram"]` / `res["near_misses"]` to the existing formatters.
- B5 Rewrite the weak histogram tests. Required cases: bucket boundaries (0.0, 0.19, 0.2, 0.39, 0.4, 0.99, 1.0); near-miss window inclusive at 0.2 and exclusive at 0.4; pairs deduplicated; `days` honored (a unit older than the window is excluded); same-owner pairs and pairs more than 48h apart excluded; empty-entity units counted in `skipped_empty`; no doubled label prefix.
- Commit subject: `audit-fix B: floor bucketing, days honored, deduped near-misses`

### Step C: story metrics (fixes D6, pure functions only, not yet wired in).
- C1 `compute_story_metrics(stories, story_units, unit_info) -> list[dict]`. Inputs: `stories` = list of dicts `{id, status, tier1_unit_count, distinct_owners}` (status as a plain string); `story_units` = dict story_id -> list of unit ids; `unit_info` = dict unit_id -> `{"source_tiers": {...}, "tier1_owner_groups": {...}}`. Per story return `{id, status, linked_units, computed_tier1_count, computed_distinct_owners, stored_tier1_count, stored_distinct_owners, mismatch}` where computed_tier1_count = sum of `source_tiers.get("tier1", 0)` over linked units and computed_distinct_owners = size of the union of `tier1_owner_groups` keys. `mismatch` = stored pair differs from computed pair.
- C2 `summarize_story_metrics(metrics, max_examples=10) -> dict` with `crosstab` ({(linked_units, computed_distinct_owners): {status: count}}), `statuses` (sorted list), `mismatch_count`, `examples` (at most max_examples mismatching rows).
- C3 Tests on hand-built inputs: a story with no links is (0, 0); the tier-1 count is the sum of `source_tiers["tier1"]`, not the number of linked units; a stale stored value is flagged; the crosstab counts by status.
- Commit subject: `audit-fix C: story metrics and stored-vs-computed`

### Step D: report assembly (fixes D3, pure functions only).
- D1 Add `format_crosstab(summary) -> str` and `format_per_day(rows, title) -> str`, and `build_report(data, days, host, now=None) -> str` that returns the ENTIRE markdown. Headings, each exactly once: `# Story Audit Report (last N days)` (report header), `## 1. Counts`, `## 2. Per-day activity (all time)`, `## 3. Story shape: linked units x tier-1 owners`, `## 4. Entity-set size per unit`, `## 5. Cross-owner Jaccard`, `## 6. Duplicate (source_domain, title)`. Include the approximation note (alias merging is not done, so overlap may be understated) and the host line. Document the `data` keys in the docstring: counts (dict), stories_per_day, articles_per_day, units_per_day, orphans_per_day (lists of `{day, status, count}`), story_summary, entity_sizes, jaccard (the B3 dict), duplicates.
- D2 Section 2 shows stories created, articles fetched, units created per UTC day, plus stories with no linked units by created day and status. Section 4 shows min/median/max and the number of units with an EMPTY set. Section 5 shows the histogram (including 0.0), the four stats, and the near-miss table. Section 6 shows at most 25 rows.
- D3 Tests: each heading appears exactly once, the report header exactly once, an all-empty `data` renders without crashing.
- Commit subject: `audit-fix D: build_report single-report assembly`

### Step E: SELECT-only data layer and wiring (fixes D3 to D5). No DB tests are possible here; the reviewer will run it against a synthetic Postgres.
- E1 `fetch_audit_data(session, days) -> dict` with SELECT statements only, producing exactly the keys documented in D1. Counts: raw_articles, reporting_units, story_unit_links, stories, stories with no linked units, units whose representative article is missing, articles with `reporting_unit_id IS NULL`. Per-day tables use `func.date` on `Story.created_at`, `RawArticle.fetched_at`, `ReportingUnit.created_at` (all time, status via the enum `.value`). Unit/story data for sections 3 to 5 comes from units in the `--days` window plus all stories and links.
- E2 `run_audit` = engine -> session -> `SET TRANSACTION READ ONLY` -> `fetch_audit_data` -> rollback -> `return build_report(...)`. `main()` prints that string ONCE and appends the identical string to $GITHUB_STEP_SUMMARY when set. Remove all now-dead code (per-story dump, incremental printing, the placeholder comment).
- E3 Verify: no `print(` inside `run_audit` or `fetch_audit_data` (grep); `--help` works both ways; `uv run pytest tests/ -q` (DATABASE_URL unset) passes with the 5 integration tests skipped.
- Commit subject: `audit-fix E: SELECT-only data layer and single-report output`

## CHECKPOINT 1: STOP
Push and report: `git --no-pager log --oneline -8`, test counts, files changed, and the compare URL. Tyler will have it re-verified, then run it himself.

## Task 3: propose grouping changes (ONLY after Tyler pastes the audit output and says "go")
Do not change code. For each option, use the audit numbers to estimate how many extra cross-owner unit pairs it would match, and show 5 example pairs it would newly match so Tyler can judge whether they are truly the same event:
- (i) keep only the top-3 entities overall by frequency, as the README says;
- (ii) overlap coefficient or a min-shared-count rule instead of Jaccard;
- (iii) compare against the story's most recent unit instead of the growing union;
- (iv) frequency-ordered truncation instead of `list(set)[:10]`;
- (v) a backfill script that rewrites existing url_hash values using canonicalize_url (dry-run by default, collision report first).
Write the proposal in your reply, not in a file. Then STOP and wait for Tyler's choice.

## Deferred: do NOT do without an explicit instruction
- Decisions for Tyler: replacement for AP/Reuters (more RSS outlets vs headline-only records), and whether to keep Reddit.
- Off-limits until then: GDELT changes, embeddings/pgvector, the heartbeat mechanism, gate logic, the curation UI, expiring or deleting orphan stories, editing production data.

## Test-compatibility notes
- tests/test_story_audit.py imports the script as `from story_audit import ...` after inserting the scripts/ directory into sys.path; keep that working.
- Existing tests elsewhere patch by name `src.ingestion.run.ingest_rss_feeds / ingest_gdelt / ingest_reddit` and `src.ingestion.{rss,gdelt,reddit}.extract_article`; do not touch those modules this round.
- After `uv sync`, spaCy's en_core_web_sm may be missing locally: `uv run python -m spacy download en_core_web_sm`.

## Report format after every checkpoint
Files changed; tests run with pass/fail counts; anything surprising; what you need from me.