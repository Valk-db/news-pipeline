# Batch: canon-harden — entity + event canonicalization hardening

## Your operating rules (read all of these first)
- You are stealth, the coding agent. Your worktree/branch will be assigned at launch (fresh `procmon/batch-canon-harden` forked from main, worktree ~/workspace/wt-canon-harden).
- FIRST ACTION: prove your directory. Run `pwd` AND `git rev-parse --show-toplevel` via exec — both must equal the assigned worktree. Then write a canary file via the Write tool containing the toplevel path; if the canary lands anywhere else, STOP and report.
- You may read files outside the worktree: ~/.config/procmon/supabase-dev.env (dev DB), ~/workspace/procmon-dev/bin/pg-tunnel.py. HARD RULES: never run env/printenv/set; never print, paste, or log any credential value; never touch ~/.ssh or browser profiles.
- OpenRouter proxy is up at 127.0.0.1:8787 (your model goes through it; default model already configured).
- Python: use /home/hatch/.venvs/nptest/bin/python for pytest/ruff. Never build venvs in /tmp.
- Egress is via HTTP proxy: Python must use urllib or curl, never raw http.client.HTTPSConnection.
- Run test suites with NO_PROXY and no_proxy UNSET (export -n NO_PROXY no_proxy): this VM's literal [::1] in NO_PROXY kills httpx.AsyncClient construction and fakes failures.
- Commit early and often on your branch (long runs get SIGTERMed; uncommitted work is lost work).
- Tyler's quality bar: beautiful, efficient, elegant. Smallest correct diff, no dead code, no placeholders, no junk.
- $0 budget. Free only. Dev Supabase only (ref qzothzirwwpesafzlxtw). NEVER touch production.
- Write your final report to REPORT-canon-harden.md INSIDE the worktree (not /tmp).

## Context
Tyler: "make sure our pipeline is actually canonicalizing properly for people and the events themselves so we can group better." Two halves.

Tyler standing principle (2026-10-02): ground this batch in established science, not ad-hoc heuristics. Entity resolution and event clustering are solved problems in the literature — use union-find for the clustering, Jaccard/MinHash where sets are compared, and evaluate with a gold set (precision/recall on hand-checked merges/splits). Be a parrot when the science is settled; invent nothing novel unless the standard approach demonstrably falls short, with evidence.

## Part A: entity alias hardening
`src/utils/ner.py` has `resolve_entities_to_canonical` backed by `ALIAS_SEED` (34 entries: 11 GPE, 10 ORG, 13 PERSON) plus `_generate_aliases`. Story grouping compares canonical entity IDs, so every missed alias splits one real-world entity into several and fractures grouping.

1. MEASURE FIRST: sample recent dev articles (raw_articles, real data), extract entities, run them through the canonicalizer. Count mentions that should resolve to one canonical entity but don't. Report the miss rate overall and by type (PERSON/ORG/GPE), with the top missed surface forms as evidence.
2. Grow from evidence: add the frequent real misses to the alias seed (or extend the generator if the misses are systematic, e.g. "<last name>" for PERSON). Do not pad with guesses — every added alias must be attested in dev data or newswire-obvious. English only; non-English tables are a separate future module.
3. Check whether `_generate_aliases` is actually wired into the canonicalization path or dead code; wire it or delete it.
4. Tests: alias resolution unit tests (PERSON/ORG/GPE), including adversarial near-misses (e.g. "Washington" the person vs the place must NOT collapse).

## Part B: canonical event identity + dedup
Today there is no canonical event identity. `src/verification/narrative.py` writes SAME_EVENT_AS edges between *stories*, but `events` rows (one per geocoded story, see `scripts/backfill_globe_events.py`) never collapse — the same quake/protest covered by N stories renders N map pins.

1. Design a canonical event identity: near-identical location (coordinate grid or radius overlap) + overlapping time window + shared canonical entities (use Part A's improved canonicalizer). Same event_type preferred but not required; never merge across wildly different types (a protest and an earthquake at the same plaza are different events).
2. Implement: prefer a `canonical_event_id` self-FK on `events` (nullable, idempotent migration under supabase/migrations/) + a dedup pass that clusters duplicate events and points them at the canonical row. The map APIs (`/api/globe/events`, `/api/map/stories`) must read canonical events only (or group by canonical id) so pins collapse. Keep it honest: collapsed rows stay in the DB with their pointer, nothing is deleted.
3. Wire the dedup into the pipeline where events are created (not just a one-off script): new events check for an existing canonical match first.
4. Tests: clustering unit tests (same quake from 3 stories -> 1 canonical; protest vs earthquake same location -> 2; time-window edges).

## Live verification (required before done)
1. Part A: miss rate before/after on a fresh dev sample; show 5+ real merges (e.g. "US"/"U.S." -> United States) and confirm story grouping improved (fewer fractured stories on a spot check).
2. Part B: event counts before/after on dev; inspect 3+ collapsed clusters by hand and confirm they are truly the same real-world event; map pin counts before/after via the live API.
3. If live verification is impossible, say exactly what blocked it.

## Report (REPORT-canon-harden.md in worktree)
- Commits; worktree state. Files changed, one line each: what and why.
- Tests: new pass count; regression counts; ruff result.
- Live evidence: miss-rate numbers, alias additions with attestation, events collapsed, cluster samples, map pin counts.
- Deferred + decisions needed (with evidence).
