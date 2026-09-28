# AGENT_TASKS.md v35

Supersedes v34 (only the git workflow changed; task content is the same). v33 and the v31 draft are discarded.
Rust stays PAUSED (do not touch `pipeline-rs/`, `ci-rust.yml`, `rust-port`). Python only, on `main`.

State on main (90e6bc1): PR 1 is merged (P0-3, P1-2, P1-1, P1-5, P1-6, freshness check, `scripts/grouping_report.py`).
Verified by daily-ingest run 36374478503: euronews.com 50 ok and pbs.org 20 ok from the runner (P1-2 done), pre-extraction dedup working (BBC 97, Guardian 103 already_known), tier-2 circuit breaker opening (economist, nytimes, wsj, ft, washingtonpost), 98 new articles, 82 units, 77 stories touched, 9 queued / 68 blocked, ~6 min.

## Working rules

- **Git workflow: work directly on `main`. No branches, no PRs, and NO commits or pushes until every task in this file is done.** Leave all changes uncommitted in the working tree while you work. Then make the commit(s) and one push at the end (see "Delivery"). The "[A]/[B]/[C]/[D]" tags below are just work groupings and ordering, not PR boundaries.
- Because nothing is pushed until the end, verify locally as you go: run the full test suite and lint after each group, and paste the results. Do not weaken or skip tests.
- Paste real evidence (log lines, query output, test counts). "Tests passed" alone is not verification. Anything that needs a real GitHub run (NPR health block, timing, stats table) is collected AFTER the final push.
- **Do not change story grouping, thresholds, or gate logic without an explicit "go 7" from Tyler.**
- If a finding depends on logs you cannot read, say so; do not guess. If a finding here turns out wrong, say so and adjust.

## Corrections to the last run report (run 36374478503)

1. **"NPR broken, feeds return OK but zero entries" is wrong.** NPR showed `already_known: 30`: the feeds returned entries and every one was already in the DB. `entries_seen` is incremented only inside `process_feed_entry`, i.e. AFTER dedup, so a fully-deduped source reads `entries_seen=0`. The classifier's condition 3 (as specified in v33 P0-3.2, which is the spec's flaw, not yours) then flags it BROKEN. VERIFIED in `rss.py` and `run.py`. See P0-4.
2. **"7 Reddit subreddits got 429s"** ignores 8x403, 2x401, 1 timeout in the same row. Most Reddit failures are auth/blocks, not rate limits.
3. **Report the raw stats table, not a paraphrase.** The report shows tier-2 rows like `feed_ok: 1, feed_failed: 8 (403)`. Extraction failures are recorded as `fetch_failed:*` (trafilatura_extract.py) and feed failures as `feed_failed:*`. Confirm the table isn't conflating them, and paste the table verbatim from `$GITHUB_STEP_SUMMARY`.
4. **The Step 0c grouping verdict is still UNRESOLVED.** If the numbers were never posted in chat, post them now (see Step 0c). Do not infer merge quality from 82 units vs 77 stories.

## P0-4 Fix tier-1 BROKEN false positive (NPR)  [A]

Risk beyond NPR: `run.py` exits 1 when >=50% of tier-1 sources are BROKEN. On a quiet day with heavy dedup, four healthy sources could fail the whole workflow.

- In `_bounded_process`, record a new stat `entries_in_feed` for every entry with a URL, BEFORE the `already_known` / `circuit_open` checks. Leave `entries_seen` semantics unchanged.
- `classify_tier1_sources_broken`: use `entries_in_feed` in conditions 1 and 3. Condition 3 becomes: feeds OK but `entries_in_feed == 0` while other sources have entries.
- Tests in `tests/test_classify_broken.py`:
  - feed_ok>0, entries_in_feed>0, already_known>0, ok=0, entries_seen=0 -> NOT broken (the NPR case).
  - feed_ok>0, entries_in_feed=0, others have entries -> broken.
  - entries_in_feed>0, ok=0, already_known=0 -> broken.
  - Say which existing tests changed and why.
- Add `entries_in_feed` to the `$GITHUB_STEP_SUMMARY` table.
- Evidence: local test output now; after the final push, the `TIER-1 SOURCE HEALTH` block from a dispatched real run showing NPR OK.

## P0-5 LLM provider failure handling  [A]

Run summary says Groq key invalid and viewpoint clustering failed. Verified so far:
- Workflow passes `GROQ_API_KEY` and `CEREBRAS_API_KEY`.
- `chat_completion` catches all Groq exceptions and falls back to Cerebras; `cluster_viewpoints` catches the final `LLMError` and logs a warning. So this run's failure means Groq failed AND Cerebras was missing or also failed.
- `classify_relevance` (and check the caption-validation path) catch only `BudgetExhausted` / parse errors on Groq, so a Groq 401 there propagates instead of falling to Cerebras.

Tasks:
1. Pull the real log (`gh run view 36374478503 --log`), paste the actual Groq and Cerebras error lines. Say whether Cerebras is configured/working.
2. Make provider auth failures uniform: catch generic exceptions on the Groq path in `classify_relevance` and the caption path, fall through to Cerebras. Do not retry 401/403.
3. Emit one `::warning title=LLM provider auth failed::` annotation per run per provider, not a warning per story.
4. Tests: mock Groq 401 -> Cerebras is called and the annotation is emitted; mock both failing -> callers get their documented defaults.
5. **Tyler action (list it in your final report):** rotate the bad repo secret(s). But do NOT wait on that, and see P2.3 below first: once the key works, viewpoint clustering will start creating child stories again.

## Step 0c: grouping diagnostic (READ-ONLY)  [only if not already posted]

Use `scripts/grouping_report.py` (already on main). Scope: units/stories from the last 48h. Paste ALL of:
1. Stories touched in the last run, created vs attached-to-existing; count of stories with 1, 2, 3+ units.
2. Stories with >=2 units from distinct tier-1 `owner_group`s. For the 9 queued: title and backing outlets. For the 68 blocked: count by block reason (`gate_reason` components: admission score, tier1_unit_count, distinct_owners, harm level/threshold). Also state whether `dynamic_gate_enabled` is on or the shadow-mode `apply_tier1_gate` is deciding.
3. Canonical-entity set size per unit (0, 1-2, 3-5, 6+). Empty set ALWAYS creates a new story; report how many and from which outlets.
4. Replay: embed title + first 500 chars of each unit's representative article (model from `src/enrichment/embedding_service.py`; TF-IDF cosine on titles if it won't install). Pairs from DIFFERENT tier-1 owner groups with cosine >= 0.80: count; % in the same story; for non-merged pairs, canonical-entity Jaccard buckets (0, 0.01-0.19, 0.20-0.39, >=0.40) and 10 example pairs (both titles, both entity sets).
5. Union-growth check: for stories with >=3 units, Jaccard(new unit, union) vs max Jaccard(new unit, any single member); how many attach decisions flip at threshold 0.4.
6. Config: `top_n_entities`, threshold 0.4, 48h window.

Then say which candidate explains the merge rate, with numbers: (a) top-N entity sets differ across outlets, (b) Jaccard 0.20-0.39, (c) union growth, (d) empty sets. Propose a fix ONLY after posting numbers, then wait for "go 7". Note that euronews and pbs were first-ever ingests in this run (70 articles), which skews the blocked count; mention it.

## P1-3 NER blocks the event loop  [B]

`extract_entities_top_n` (spaCy) is called synchronously inside async `process_feed_entry` and `reddit.process_entry`. Wrap in `await asyncio.to_thread(...)`. Keep the function itself sync and pure. Compare wall time before/after from the post-push dispatched run (and locally if you can).

## P1-4 Workflows  [B]

- a) Heartbeat: `pull --rebase` and the retry loop are in; still add `continue-on-error: true` on the step and make sure a failed rebase does not leave the tree mid-rebase (`git rebase --abort || true`).
- b) Bump `setup-uv` (currently @v4), `checkout`, `setup-python` to current majors (clears Node 20 deprecation and the cache-service 400 annotations). If cache errors persist, `enable-cache: false` and say so.
- c) `ubuntu-latest` becomes Ubuntu 26 on 2026-10-19: pin `runs-on: ubuntu-24.04` in ALL workflows except `ci-rust.yml`, which you don't touch.
- d) Cron `0 6,18 * * *` starts ~4h late. Move to an off-peak minute (`23 6,18 * * *`); same for weekly-enrichment and cleanup.
- e) `sentence-transformers` (and torch index) belong in a new `enrichment` extra used only by weekly-enrichment; daily ingest must not install torch. Regenerate `uv.lock`.
- f) Pin the `en_core_web_sm` wheel in `pyproject.toml`/`uv.lock` instead of `spacy download` per job.

## P2 Rest of pipeline  [C]

1. Weekly enrichment: read the logs of the last passing scheduled run for all four jobs (`enrichment`, `globe-backfill`, `phase2`, `reliability`); report `stories_processed`, `total_media_assets`, `total_embeddings_generated`. Fix anything silently zero or erroring.
2. `weekly-enrichment.yml`: `SUMMARY='${{ steps.enrichment.outputs.ENRICHMENT_SUMMARY }}'` inlines data into a single-quoted shell string (breaks on an apostrophe, injection vector). Pass via `env:` and use `"$SUMMARY"`. Check other steps for the same pattern.
3. **`cluster_viewpoints` duplicate-child risk (VERIFIED in code, higher priority now).** It creates a NEW `Story` row per viewpoint label for every story with >=3 units, with no check for existing children (`viewpoint_cluster_id == story.id`), and it re-runs on any parent modified in a later run. It is currently dormant only because the LLM call is failing (P0-5). Once the key is fixed it will start emitting duplicate PENDING children that also go through the gate. Report: how many viewpoint child stories exist now, and whether any parent already has duplicates. Then add an idempotency guard (skip parents that already have children, or reuse/replace them) with a test. This is a bug fix, not a grouping-logic change; if you think the guard changes what stories users see, stop and ask for "go 7".
4. Read `status_log` rows for `verify`, `group`, `gate` from the latest runs; confirm none errored.
5. `cleanup.yml` runs with `--queued-hours=0`: confirm it isn't expiring stories the curation UI still needs. Report what the last cleanup run removed.
6. Tier-2 is effectively dead (5 of 6 domains got zero articles: economist/nytimes 403, wsj 401, ft 403, washingtonpost timeouts; latimes feed 403). Find every downstream use of tier-2 units (corroboration, admission score) and report whether a story can gain anything from tier-2 today. **Report only, no behavior change.** Include options (RSS title/summary as lightweight corroboration; disable domains after N consecutive dead runs; leave as-is) and what each would do to gate outcomes.
7. Reddit: report exact per-subreddit outcomes for the run and reconcile the counts (18 failed outcomes vs 11 subs). Check whether retries are double-recorded in `STATS`. If Reddit is just blocking runner IPs (403/401), say so; if more than half the subs still fail, cut `TARGET_SUBREDDITS` to the ones that succeed with a comment. No auth or scraping.

## P3 Cleanup  [D, after A-C]

- **NPR feed reconciliation first:** `source_registry.py` lists NPR `1001, 1003, 1004`; the legacy `TIER1_FEEDS` in `rss.py` lists `1001, 1003, 1014` ("Politics, verified working 2026-09-24"). Fetch all four (1001, 1003, 1004, 1014), record HTTP status, entry count and the feed's own `<title>`. Fix the registry accordingly.
- Diff EVERY URL in `TIER1_FEEDS` against `source_registry.py` and reconcile (the stale copy was already the correct one twice).
- Then, if nothing calls `ingest_rss_feeds(sources=None)` (grep to confirm), delete `TIER1_FEEDS` and the `sources=None` path.
- Record `entries_seen` once per unique entry, before extraction, consistently for RSS and Reddit. (Coordinate with P0-4's `entries_in_feed`; keep the two stats distinct and documented in the code comment.)
- `README.md` source list is stale vs the registry (8 tier-1, 12 tier-2, tier-3 Reddit + Bluesky, tier-4 Substack). Regenerate it from the registry or replace it with a pointer.

## Delivery

1. Do the work in this order: A (P0-4, P0-5), Step 0c numbers (post in chat if not already posted; no need to wait for a reply), B (P1-3, P1-4), C (P2), D (P3). Everything stays uncommitted on `main`.
2. When all tasks are done: full test suite + lint one final time, paste the counts.
3. Commit (separate commits per group are fine, with subject + body explaining the *why*), then `git pull --rebase origin main` (the heartbeat bot pushes to main, so expect this), then push once. Never push red.
4. After the push, dispatch daily-ingest and paste: the `TIER-1 SOURCE HEALTH` block, the per-source stats table verbatim, new-article count, Phase 2/3/4 counts, duration. Also confirm the workflow changes from P1-4 actually took (no cache 400s, pinned runner, off-peak cron). If the real run exposes a problem, fix it and push a follow-up straight to `main`.
5. Final message: per-task evidence, plus one clearly marked block for Tyler actions (rotate LLM secret(s); any "go 7" decision pending).