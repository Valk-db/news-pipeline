# AGENT_TASKS.md v24

Supersedes v23. Narrower scope on purpose — T2/T3's files now exist with real
content (verified: OWNERSHIP_GROUPS matches Python exactly, tiers.rs gate logic
is real). Two things don't: nothing is wired end-to-end, and a specific false
claim from v22's review was repeated instead of corrected. This batch fixes both
before touching T4 or moving forward on anything else.

**Standing rule, sharpened:** "complete" means the binary actually runs it, not
that the module compiles with its own unit tests. Going forward, before
reporting a task done: run `cargo run` (or a small test binary) against a real
or scratch database and paste actual output — row counts, IDs, something
concrete — into the PR description. Unit tests passing is necessary, not
sufficient.

**Also:** don't restate a claim from a prior review as fact without checking it
again first. If the last review said X wasn't there, verify it's there now
before saying so — a repeated claim isn't more true for having been said twice.

---

## W1 — Wire the pipeline together

- `main.rs` (or a new `bin/run_pipeline.rs`, your call) should actually call
  `ingestion::run_ingestion(...)`, then feed its output into
  `verification::units`, `verification::stories`, `verification::tiers::apply_dynamic_gate`,
  in that order — mirroring whatever order `scripts/` currently invokes the
  Python equivalents in.
- Run it against a scratch Supabase project (or a local Postgres with the same
  schema) with a handful of real RSS sources. Confirm rows actually land in
  `reporting_units` / `stories` and the gate actually blocks/queues something.
- This is the actual definition of done for T2/T3 — not "files exist and their
  own tests pass."

## W2 — Fix the fact_checker.rs claim, for real this time

- Either implement the embeddings API-fallback path in `fact_checker.rs` for
  real, or remove the claim entirely and say plainly it doesn't exist yet.
  Don't repeat the sentence again without one of those two things being true.

## T5 — actually attempt it (still not done)

- `Cargo.toml` still has only unused `candle`. Add `rust-bert` and attempt its
  NER pipeline for real (either `tch` or `ort` backend) — if both genuinely
  fail to build, paste the actual `cargo build` error into the PR description.
  A commit message repeating "couldn't be used due to yanked ort dependencies"
  without that error attached isn't verifiable and won't be taken as sufficient
  this round.
- Same standard for embeddings (`rust-bert`'s sentence-embeddings pipeline) and
  extraction (`trafilatura`, `readex`, `justext`, or `libreadability` — none
  are in `Cargo.toml` yet; the current `extract_from_html` regex tag-stripper
  needs to be replaced, not left as-is).
- Remove `candle` from `Cargo.toml` if it ends up unused after this.

Do not proceed to T4/Reliability integration until W1 and W2 are done and
verifiable — not until they're reported done.