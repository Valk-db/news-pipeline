# AGENT_TASKS.md v36

Supersedes v35. Rust stays PAUSED (do not touch `pipeline-rs/`, `ci-rust.yml`, `rust-port`). Python only, on `main`.

State on main (1ae9d7a, includes your commit 8584913). Verified from a fresh clone and run 36379797179: P0-4 works (all 8 tier-1 OK, NPR OK with entries_in_feed=30), P1-3 done, ubuntu-24.04 pinned, off-peak crons, `enrichment` extra split out (daily ingest no longer installs torch), SUMMARY injection fixed, P2.3 guard is in the code.

## Working rules

- **Git workflow: work directly on `main`. No branches, no PRs, NO commits or pushes until every task in this file is done.** Then commit, `git pull --rebase origin main` (the heartbeat bot pushes to main), push once, dispatch daily-ingest, and paste results (see Delivery).
- Run the full tests and lint locally after each group and paste the counts. **Never delete, weaken, or skip tests** (see Task 1: last round broke this rule).
- Do not report a task done unless you paste the evidence the task asks for. A status table without numbers is not evidence.
- **Do not change story grouping, thresholds, or gate logic without an explicit "go 7" from Tyler.** Step 0c work below is read-only and offline.
- If a finding depends on logs you can't read, or turns out wrong, say so; do not guess.
- Do not put tasks in the "Tyler actions" list that are yours. Tyler's actions are ONLY: rotate/set LLM secrets, and give (or withhold) "go 7".

## 1. RESTORE DELETED TESTS (do this first, blocking)

Commit 8584913 deleted `tests/test_viewpoint_clustering.py` (487 lines, 9 tests) and added a 3-test idempotency file. None of the 9 test names exist anywhere else in `tests/` (verified by grep). They were:
`test_cluster_viewpoints_basic`, `test_cluster_viewpoints_insufficient_units`, `test_viewpoint_substories_blocked_by_tier1_gate` (a P0.2 regression test: tier-3-only parents must yield BLOCKED sub-stories), `test_viewpoint_cluster_model_fields`, `test_source_tier_enum`, `test_tier_definitions`, `test_source_registry`, `test_tiered_scheduler`, `test_recompute_story_counters_extended`.

Only 3 of them relate to `cluster_viewpoints`; the other 6 cover tiers, registry, scheduler and counters and had nothing to do with P2.3. The "387 passed" figure was reached partly by removing tests.

- Restore the file: `git show 8584913^:tests/test_viewpoint_clustering.py > tests/test_viewpoint_clustering.py`.
- The most likely reason they failed: the new P2.3 guard does an extra `session.execute(select(func.count(...)))` that the old mocks don't supply. Fix the MOCKS (add a child-count result returning 0 for the "no existing children" path), not the assertions and not the guard's behavior.
- Keep the new idempotency tests alongside.
- Evidence: full suite count before and after (expect at least 387 + 9 - overlap), and `pytest tests/test_viewpoint_clustering.py -v` output. If any restored test genuinely conflicts with intended new behavior, stop and explain in chat; don't delete it.

## 2. Finish the unfinished tasks from v35 (all still open)

### 2a. P3 (was "deferred until after push", which was not permitted)
- NPR feed reconciliation: registry lists `1001, 1003, 1004`; legacy `TIER1_FEEDS` lists `1001, 1003, 1014`. Fetch all four, record HTTP status, entry count and each feed's own `<title>`. Fix the registry accordingly. (This is your task, not Tyler's.)
- Diff EVERY URL in `TIER1_FEEDS` against `source_registry.py` and reconcile.
- If nothing calls `ingest_rss_feeds(sources=None)` (grep to confirm), delete `TIER1_FEEDS` and the `sources=None` path. Also fix the two comments in `tiers.py` (lines ~83-84) that refer to it.
- Regenerate the README source list from the registry (8 tier-1, 12 tier-2, tier-3 Reddit + Bluesky, tier-4 Substack) or replace it with a pointer.

### 2b. P1-4 items skipped or done partially
- **b) Actions versions.** You bumped everything to "v5", but the reason for bumping (Node 20 deprecation) is not cleared: `actions/checkout@v4` and `actions/setup-python@v5` are still on Node 20. GitHub's API shows current majors are `actions/checkout` v7 and `actions/setup-python` v7 (verify with `gh api repos/actions/checkout/releases/latest`; also find the current `astral-sh/setup-uv` major from its tags). Bump all workflows (not `ci-rust.yml`) and paste the resulting run's annotations.
- **Cache errors.** You wrote off the persistent cache 400s as "known, harmless". The instruction was: if they persist after the bump, set `enable-cache: false` and say so. After the version bump, if the annotations remain, set `enable-cache: false` in daily-ingest and paste the before/after annotation list and run time. Don't leave it as a note.
- **f) Pin `en_core_web_sm`.** Still `uv run python -m spacy download en_core_web_sm` in `ci.yml` and `daily-ingest.yml`. Pin the wheel as a dependency in `pyproject.toml`/`uv.lock` (the release URL for the spaCy version in `uv.lock`) and remove the download step from both.

### 2c. P2 report items with no evidence
Post the actual numbers in chat for each of:
1. **P2.1 weekly enrichment:** logs of the last passing scheduled run, all four jobs (`enrichment`, `globe-backfill`, `phase2`, `reliability`): `stories_processed`, `total_media_assets`, `total_embeddings_generated`. Fix anything zero or erroring. NOTE: the workflows changed this round (enrichment extra), so also confirm the next dispatched `weekly-enrichment` run installs and imports `sentence-transformers` correctly. Dispatch it after the push and paste the four job outcomes.
2. **P2.3 (extra):** report how many viewpoint child stories exist now and whether any parent already has duplicate children from before the guard. If duplicates exist, report the count; do not delete data without asking.
3. **P2.4** `status_log` rows for `verify`, `group`, `gate` from the latest runs: paste the rows or counts, not "OK".
4. **P2.5 cleanup:** paste what the last cleanup run removed (counts by status). Decide with evidence whether `--queued-hours=0` expires stories the curation UI still needs. "Safe" without numbers isn't accepted.
5. **P2.6 tier-2:** find every downstream use of tier-2 units (corroboration, admission score) and say whether a story can gain anything from tier-2 today, given 5 of 6 tier-2 domains produce zero articles. Report only; give the three options (RSS title/summary as light corroboration; disable domains after N consecutive dead runs; leave as-is) and what each does to gate outcomes.
6. **P2.7 Reddit:** exact per-subreddit outcomes. Your own numbers disagree: the table shows 7x429 + 6x403 + 1x401 (14), the notes say 8x429 + 6x403 + 1x401 + 1 timeout. Reconcile against the log and check for double-recording of retries in `STATS`. **If more than half the subs fail, cut `TARGET_SUBREDDITS` to the ones that succeed with a dated comment. Do it; don't hand it to Tyler.** No auth or scraping.

## 3. Stats accuracy bugs (found in the run 36379797179 table)

1. **The table wasn't verbatim.** The report shows `feed_failed: 8 (403)` for tier-2 domains with `feed_ok: 1`. The code records extraction failures as `fetch_failed:http_403` (`trafilatura_extract.py`), and `feed_failed:*` only for feed-level failures. Paste the real `$GITHUB_STEP_SUMMARY` table, and make sure the summary renderer shows `fetch_failed:*` and `feed_failed:*` as separate columns (or clearly labeled), not merged.
2. **`too_short` is mislabeled.** It equals the number of failed extractions for every blocked domain (economist 8, ft 8, nytimes 9, wsj 9, washingtonpost 9): `rss.py:~201` records `too_short` whenever `process_feed_entry` returns nothing, including on 403/timeout. Record `too_short` only when the extracted text is actually below the minimum length; extraction failures are already counted as `fetch_failed`. Same check in `reddit.py:~73`. Test it.
3. **`circuit_open` counts the tripping event as a skipped entry** (`rss.py:~350` records it when the breaker opens, and again for every skipped entry). Result: `entries_seen + circuit_open` exceeds `entries_in_feed` by 1-2 for every blocked domain (economist 8+43 vs 50, nytimes 9+61+1 already_known vs 69, wsj 9+33 vs 40, ft 8+5 vs 12, washingtonpost 9+8 vs 15). Record the trip as its own stat (`circuit_tripped`) and keep `circuit_open` = entries actually skipped. Add a reconciliation invariant test: `already_known + entries_seen + circuit_open == entries_in_feed` for a domain after processing (allowing for in-flight concurrency, document how you handle that).

## 4. Step 0c follow-up: make "go 7" an informed decision (READ-ONLY, offline)

Tyler has not yet seen the numbers behind your claim. Your summary says: 6 cross-outlet pairs, 33% merged, entity Jaccard 0.12-0.29 despite title cosine 1.0, cause = top-N entity sets differ. **Six pairs is too small a sample to change grouping.** Re-run `scripts/grouping_report.py` over the widest window available (7 days or all data), and post:
1. Number of cross-owner pairs with cosine >= 0.80 and the merge rate. Also the merge rate at cosine >= 0.90.
2. Jaccard distribution for non-merged pairs (buckets 0, 0.01-0.19, 0.20-0.39, >=0.40), and how many merged pairs sit just above 0.40.
3. **What-if simulation (no production change):** recompute each unit's entity set at `top_n_entities` = 3, 5 and 6, and recompute the merge decisions for the same window. For each setting report: pairs that would newly merge; and the FALSE-MERGE risk, i.e. how many newly-merged pairs have cosine below 0.6, plus 10 example pairs (both titles) among the newly merged. Also report the union-growth flip count (Step 0c item 5) at each setting.
4. Recommend one option or "none", with the numbers. Then STOP and wait for Tyler's "go 7".

## 5. LLM secrets (Tyler's action, but confirm your side)

Groq returns 401 on every call and no Cerebras key is set, so viewpoint clustering has been dormant. Before Tyler rotates the secrets, confirm from the code that everything that calls the LLM degrades cleanly when BOTH providers fail (classify_relevance, generate_caption, chat_completion): each returns its documented default and the run continues. Cite the tests. Also confirm that the P2.3 guard is exercised by a test against real query semantics, not only mocks (an SQLite-backed test is fine: create a parent with one child, run `cluster_viewpoints` twice, assert one child set).

## Delivery

1. Order: Task 1 (restore tests), 2a, 2b, 3, 2c, 4, 5. Everything stays uncommitted on `main` until all are done.
2. Final full test suite + lint; paste counts. The test count must not go down versus main.
3. Commit (separate commits per group are fine, subject + body explaining the *why*), `git pull --rebase origin main`, push once. Never push red.
4. After the push: dispatch daily-ingest and paste the tier-1 health block, the real per-source table (with `fetch_failed`, `feed_failed`, `too_short`, `circuit_open`, `circuit_tripped` as the code now records them), new-article count, Phase 2/3/4 counts, duration, and the run's annotations (Node 20 warning, cache errors). Dispatch weekly-enrichment and paste the four job outcomes. If either exposes a problem, fix it and push a follow-up straight to `main`.
5. Final message: per-task evidence, then a block titled TYLER ACTIONS containing only: (a) rotate `GROQ_API_KEY` and set `CEREBRAS_API_KEY`; (b) "go 7" decision, with your recommendation from Task 4.