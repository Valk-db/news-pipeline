# AGENT_TASKS.md v23

Supersedes v22. v22 assumed T1-T5 would be attempted faithfully against the spec;
this revision corrects task status after checking the rust-port branch directly
against what T2/T3/T5 actually named, plus one new standing rule below.

**New standing rule — reporting status:** a task is "complete" only when every
file it names exists with real logic (not a placeholder/regex-fallback) *and*
is actually called from somewhere reachable from `main.rs` — a file compiling
in isolation with its own unit tests doesn't count. Don't describe something as
"implemented in <file>" unless you can point to the actual code — that
specific claim was checked this round and wasn't true. If a task is partial,
report it as partial and name exactly what's missing — that's not a worse
outcome than claiming completion, it's the only useful signal Tyler can act on.

**Merge policy, branch (`rust-port`), and everything in the Infra section:**
unchanged from v22 — still correct, not revisited here.

---

## T1 — unchanged, holds up

Confirmed real: `config.rs`, `database.rs`, `llm.rs`, `models.rs`, and `main.rs`
actually exercises the pool + `SELECT 1`. Nothing further needed here.

## T2 — finish the missing three files

`rss.rs`, `reddit.rs`, `source_registry.rs` exist and look real. Still missing,
as originally scoped:
- `adapter.py` → `adapter.rs`
- `tiered_scheduler.py` → `tiered_scheduler.rs`
- `run.py` → `run.rs` (the actual ingestion entry point / orchestration)

## T3 — write the missing grouping/tiering logic, dedupe the Jaccard math

`ner.rs`'s canonicalization layer is done and good. Still missing:
- `tiers.py`, `topics.py`, `cleanup.py`, `stories.py`, `units.py` — the actual
  grouping/tiering logic that *calls* `get_primary_entity_set` /
  `entity_set_jaccard` / `canonical_jaccard` on real ingested articles. Until
  these exist, the canonicalization layer isn't reachable from anything.
- `verification/utils.rs` duplicates `entity_jaccard`/`canonical_jaccard` that
  already live in `utils/ner.rs` under different names. Delete the duplicate,
  have `stories.rs`/`units.rs` import the one in `ner.rs`.

## T4 — spot-check, not re-done

Files exist. Before treating this as settled: confirm `claims.rs`/`narrative.rs`
are actually called from something reachable from `main.rs`, not just compiling
with their own tests, same standard as everything else here.

## T5 — actually attempt it this time

1. **NER** — `extract_entities_top_n` is currently an empty-dict placeholder.
   Attempt `rust-bert`'s NER pipeline for real, either backend (`tch` or `ort`).
   If both backends genuinely fail to build, that's a legitimate outcome — but
   it needs to be a real build attempt with the actual error output kept in
   the PR description, not a placeholder function with a comment. Still watch
   for the GPE-vs-LOC / OntoNotes label issue flagged in v22.
2. **Embeddings** — nothing exists yet despite the last report. Attempt
   `rust-bert`'s sentence-embeddings pipeline for `all-MiniLM-L6-v2` (or
   nearest available preset) for real. `candle` is already in `Cargo.toml` but
   unused — either wire it to something or remove it.
3. **Extraction** — `extract_article` currently strips HTML tags with a
   regex, which is materially worse than doing nothing here since it will
   silently pull in nav/ads/footers as "article text." Add one of `trafilatura`,
   `readex`, `justext`, or `libreadability` to `Cargo.toml` and wire it in for
   real, then spot-check against a handful of real URLs from your actual RSS
   sources before calling it done.

For all three: if a real attempt hits a genuine blocker (crate yanked, C++
toolchain issue), document the exact error and what was tried in the PR
description as before — that part of the process was fine. The problem last
round wasn't documenting a blocker, it was marking the task complete anyway.