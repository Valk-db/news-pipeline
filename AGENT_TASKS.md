# AGENT_TASKS.md v29

Supersedes v28. P0 confirmed real and done. This batch: get an honest read on
the model-loading failure (don't accept "no network access" without the real
error text), and clean up the main.rs duplication while it's already being
touched.

## P1a — Get the real error, don't assume the cause

- Pull the actual `error!("Failed to load NER model: {}", e)` /
  equivalent embedding-model line from the CI 36356719994 logs and paste it
  verbatim. If it's a connection/DNS failure, "no network access" holds. If
  it's a 404, a redirect, or an HTTP error against a specific URL, that's a
  stale-URL problem in rust-bert's resource resolution, not a network block —
  different fix (possibly overriding the resource URL manually, or checking
  for a newer rust-bert release).
- Once the real cause is known: fix it if fixable, or document precisely why
  it isn't, before calling T5 functionally verified either way.

## P1b — Fix main.rs's module duplication

- `main.rs` declares its own `mod config; mod database; ...` tree duplicating
  `lib.rs`. Change it to `use pipeline_rs::{config, database, ...};` (or
  equivalent) so the binary depends on the library crate instead of
  recompiling the same source separately. Confirm the test count drops back
  to a single count (not lib+bin duplicated) and note whether build time
  visibly improves.

Once P1a's real cause is known, T5's actual functional verification (GPE/LOC
correctness, real embedding output) from v28 is still the next thing after —
not replaced by this batch.