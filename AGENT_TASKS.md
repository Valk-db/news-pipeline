# AGENT_TASKS.md v37

Supersedes v36. pipeline-rs/ and ci-rust.yml were REMOVED 2026-10-01 per Tyler (frozen divergent fork; full history recoverable via git). Python only, on `main`.

## Verified from a fresh clone (no work needed)

Tests restored with no deletions (398 passed reported); NPR registry now has 4 feeds and `TIER1_FEEDS` is deleted; checkout/setup-python at v7 and setup-uv at v10.0.0; `circuit_tripped` split from `circuit_open` and the stats invariant holds in the run data (economist 8+42=50, nytimes 1+9+60=70); `too_short` no longer mirrors failures; cleanup's `--queued-hours=0` means DISABLED in `cleanup_stale.py` (default 0 = opt-in), so queued stories are safe and the 473 expired were PENDING/BLOCKED; tier-2 is not used by the tier-1 gate (`tiers.py` comment "tier2 doesn't matter for the gate").

## Working rules

- **Git workflow: work directly on `main`. No branches, no PRs, NO commits or pushes until every task in this file is done.** Then commit, `git pull --rebase origin main`, push once, dispatch, paste results (see Delivery).
- **Run EXACTLY what CI runs before pushing:** `uv run ruff check .`, `uv run mypy src/ curation_ui/`, `uv run pytest tests/`. Last round you ran `ruff check src/` only, pushed `scripts/what_if_sim.py`, and needed a follow-up lint-fix commit (cf769e7). Paste all three outputs. Also report the GitHub Actions result of `ci.yml` for the last 5 commits on `main` (`gh run list --workflow ci.yml --branch main --limit 5`).
- Never delete, weaken, or skip tests. Test count must not go down.
- **Never print secret values** (API keys, tokens, DB URLs) in chat, reports, commits or logs. Mask them. See Task 1.
- Do not report a task done unless you paste the evidence it asks for. Placeholder text ("+newly merged pairs, false merge risk") is not evidence.
- **Do not change story grouping, thresholds, or gate logic without an explicit "go 7" from Tyler.**
- If something timed out, failed, or you could not do it, say so in the report. Last round's commit message admitted the what-if simulation timed out; the final report presented it as done.
- Tyler's action list contains ONLY: rotating secrets and the "go 7" decision. Everything else is yours.

## 1. Secret hygiene (do first)

Your final report printed a full Cerebras API key in chat. Treat that key as exposed; Tyler will rotate it. Do not repeat it anywhere. Also answer, without printing any secret value:
1. Where did you get the value? (Repo secrets can't be read by `gh`, so presumably a local `.env`.) 
2. Is `CEREBRAS_API_KEY` actually set as a repo secret? `gh secret list` shows names only; paste the name list. Last round's report said it was unset; this one says "configured in secrets".
3. Confirm no secret value is committed anywhere in the repo or in `what_if_output.txt` (delete that empty stray file at the repo root; it was committed in 88ef728). Add `*_output.txt` or the specific file to `.gitignore` if it is a recurring artifact.

## 2. Task 4 was NOT done: run the what-if simulation properly (READ-ONLY, offline)

Facts: commit 88ef728 says the sim "timed out, partial". The report's what-if section has placeholders and no numbers, and it contradicts itself (55 pairs / 16.4% merged vs "6 pairs merged, 28.6%"). The "Recommendation: none, wait 30+ days" is therefore unsupported. Also it used TF-IDF on titles only; Step 0c specified embeddings on title + first 500 chars, with TF-IDF only as a fallback that must be stated.

What the numbers you did report actually show: of 46 unmerged likely-same-event pairs (cos >= 0.80), 25 are at entity-Jaccard 0.01-0.19 and 21 at 0.20-0.39; none at 0 and none >= 0.40 (which is expected: those would have merged). So the split is not "sample too small"; the open question is the trade-off. Answer it with data:

1. Find out why it timed out (DB round-trips per unit? embedding model load? O(n²) in Python?). Fix that: pull the last 7 days of tier-1 units + representative articles + entity sets ONCE into a local pickle/JSON snapshot, then run all analysis offline against the snapshot. Don't re-query the DB in loops. 1,554 units is small; a full pairwise cosine matrix is fine with numpy.
2. State which similarity you used (embeddings vs TF-IDF) and why.
3. For `top_n_entities` = 3, 5, 6 report, from the same snapshot: cross-owner pairs at cos >= 0.80 and >= 0.90; pairs merged at threshold 0.4; pairs that would newly merge vs the current setting; and **false-merge risk = newly merged pairs with cos < 0.6**, with 10 example pairs (both titles) for each setting. Also report the effect of lowering the Jaccard threshold (0.3, 0.25, 0.2) at top_n=3, same metrics.
4. Union-growth flips at 0.4 for each setting (you reported only top_n=3: 9).
5. Recommend one option or "none", justified by these numbers. Then STOP and wait for Tyler's "go 7". Do not modify grouping code.

## 3. Task 5 claim was false: add a real idempotency test

The report says the P2.3 guard is tested by a SQLite-backed test that "creates a parent with 1 child, runs cluster_viewpoints twice, asserts one child set" and cites `test_viewpoint_substories_blocked_by_tier1_gate`. Neither is true: that test and all three tests in `test_viewpoint_clustering_idempotency.py` use `AsyncMock`/`MagicMock`, no database (`aiosqlite` is already in the dev deps). Mock tests can't tell whether the guard's query is correct.

- Add a real test using an in-memory SQLite (or the CI Postgres service if the models don't run on SQLite; say which and why): create a parent story with >= 3 units, stub only the LLM, run `cluster_viewpoints` twice, assert the number of child stories is identical after the second run and no duplicates per label.
- Also state, with the test names, what each LLM path returns when BOTH providers fail (classify_relevance, generate_caption, chat_completion). "Returns empty/default" needs test names, not a summary.

## 4. Tier-2 (P2.6) and dead domains: garbled report

The report says "0 of 6 active tier-2 domains produce zero articles (all 6 have articles; 5 disabled)". This contradicts run 36452503330 where economist, nytimes (and per earlier runs wsj, ft, washingtonpost) are still enabled and hit the breaker every run, and the registry only shows a dated disable at one entry (line ~446) plus two "no working RSS" entries.

1. Table, one row per tier-2 domain: `enabled` flag, feed URL, ok / attempts / already_known from the last 5 daily-ingest runs (`gh run view --log`), and article count in the DB for the last 7 days.
2. Apply the v33 P1-5 rule now: **disable (`enabled=False`, dated comment with the evidence) every domain with ~0 ok across ALL of the last 5 runs.** Don't touch any domain with ok > 0 in a majority of runs (foreignpolicy, foreignaffairs, csis, who, un are producing). This saves ~40s per run and removes noise from the tier-2 rows.
3. Keep the tier-2-usage finding (virality signal only, not corroboration or gate) but cite the file:line for it.

## 5. Reddit: cut not done, evidence not per-sub

You wrote "recommend cutting", from "DB stats", and named worldnews/geopolitics/news without per-subreddit data. Last round's instruction was to do the cut yourself if more than half fail.
1. Per-subreddit outcomes (sub, status: ok / 429 / 403 / 401 / timeout) for each of the last 3-5 daily-ingest runs. If the logs/stats don't record outcomes per subreddit, add a per-sub stat/log line (e.g. `reddit.<sub>` -> `feed_ok` / `feed_failed:http_XXX`) and say so.
2. If per-sub data exists for >= 3 runs: cut `TARGET_SUBREDDITS` to the subs that succeeded in at least half of those runs, dated comment listing the dropped ones. If it doesn't exist yet: add the recording, leave the list as-is, and say the cut waits for the next 3 runs (Tyler's not deciding this; you will do it next round).
3. Reconcile the counts: latest run has fetch_failed 15x403 + 8x429 + 1x401 + 1 timeout = 25 outcomes against 11 subs and `feed_ok: 3`. Retries inflating the counts? Check `STATS` recording.

## 6. Workflow leftovers

1. **spaCy model:** `pyproject.toml` pins the `en_core_web_sm` wheel AND both `daily-ingest.yml` and `ci.yml` still run `spacy download en_core_web_sm` (commit 4267e7b added it back "because the URL didn't work in CI"; the 88ef728 message says the step was removed). That leaves two sources of truth. Find the exact CI error (`gh run view --log-failed` for the run before 4267e7b), fix it (the `uv.lock` change in cf769e7 may already resolve it), verify by removing the download step locally with a clean venv (`uv sync --frozen`, import the model), then remove the step from all workflows. If the pin can't be made to work, revert the pyproject pin so there's a single mechanism. Say which.
2. **Cache errors / Node 20:** `enable-cache: true` is still set. Paste the annotations from the post-push daily-ingest run. If "Failed to restore/save cache" or Node 20 warnings persist, set `enable-cache: false` in daily-ingest and say so. Don't call them harmless.
3. `TIER1_FEEDS` deleted: confirm `tiers.py` comments (lines ~83-84) no longer refer to it, and README is a pointer.

## 7. Weekly enrichment: contradictions to explain

Run 36453796551 reported `total_media_assets=0` and, in reliability, `pairs_scored=0, claims_considered=0` ("no claims yet"), while phase2 says claims were extracted. Both can't be right.
1. `SELECT count(*)` from the claims table (and media-assets table). Paste the counts.
2. If claims exist but reliability saw 0: find why (filter, join, day window) and fix.
3. If media enrichment is unimplemented or gated off, say so with file:line; if it's a bug, fix it.
4. Explain the `stories_processed=91` vs `total_embeddings_generated=91` result (matches, fine) and how many stories were skipped vs already embedded.

## Delivery

1. Order: 1, 6.1-6.3, 4, 5, 7, 3, 2. Everything stays uncommitted on `main` until all are done.
2. Final: `ruff check .`, `mypy src/ curation_ui/`, `pytest tests/`; paste all three outputs. Test count >= 398.
3. Commit (separate commits per group are fine, subject + body explaining the *why*, and an accurate message: if something was partial, say so), `git pull --rebase origin main`, push once. Never push red.
4. After the push: dispatch daily-ingest and paste ALL of: the tier-1 health block; the FULL per-source stats table, every source, verbatim (last time only 6 rows were pasted); new-article count; Phase 2/3/4 counts; duration; the run's annotations. Dispatch weekly-enrichment and paste the four job outcomes. Report ci.yml results for the pushed commit. If a run exposes a problem, fix it and push a follow-up straight to `main`.
5. Final message: per-task evidence, then a block titled TYLER ACTIONS with only: (a) rotate the exposed Cerebras key and the invalid Groq key, set both as repo secrets; (b) "go 7" decision, with your recommendation from Task 2 and its numbers.