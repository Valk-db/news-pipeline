# AGENT_TASKS.md v28

Supersedes v27. Comprehensive pass — covers the reported test hangs plus
everything else still open across the migration. Priority order below;
work top to bottom.

**Standing rules, unchanged:** self-merge once checks pass, don't wait for
review. "Complete" means real output from a real run pasted as evidence, not
a compile success. Don't restate a prior claim as fact without rechecking it.

---

## P0 — Fix the test hangs

1. **Decouple the worker thread from any specific tokio runtime.** Replace
   `tokio::task::spawn_blocking` for the NER/embedding workers with a plain
   `std::thread::spawn` — a raw OS thread isn't tied to any Runtime's
   lifecycle, so it survives fine across per-test runtime creation/teardown.
   The async side (`oneshot` response, `mpsc` request) still works talking to
   a plain thread; only the *spawning mechanism* needs to change.
2. **Fix the double-init race.** Swap `std::sync::OnceLock` for
   `tokio::sync::OnceCell` (async-aware `get_or_init`), so two concurrent
   callers can't both pass the check before either finishes initializing.
3. **Cache the model weights in CI.** Add `~/.cache/.rustbert` (or wherever
   `RUSTBERT_CACHE` is pointed, if it's set explicitly anywhere) to the
   `actions/cache` path list in `ci-rust.yml`, keyed on the model
   name/version so a cache hit skips the download entirely.
4. **Add a timeout to the `cargo test` step**, matching the pattern already
   used for `cargo run` elsewhere in the same workflow (`timeout 180` or
   similar) — so a real hang fails fast with a clear signal instead of
   burning CI minutes silently.
5. Verify the fix by actually triggering the NER/embedding path from a test
   (even a throwaway one) and confirming the run completes and the runtime
   tears down cleanly — not just that unrelated tests still pass.

## P1 — T5 functional verification (still open since v22)

- Confirm on 5-10 real articles: does the NER output preserve GPE vs LOC
  distinctly (OntoNotes-style), or does `dslim/bert-base-NER` collapse them
  to a CoNLL-style LOC/MISC scheme? Paste the actual entity output.
- Confirm the embedding output is genuinely 384-dim and looks like a real
  sentence embedding (not all-zero/NaN from a failed load silently swallowed
  by the error-handling branch in the worker).

## P1 — T4 audit (never actually verified, unlike T2/T3)

`claims.rs`, `narrative.rs`, `consensus_analyzer.rs`, `fact_checker.rs` exist
and compile, but — unlike T2/T3 — nothing has confirmed they're wired into
the real pipeline flow or produce sane output against real data. Check:
- Are these actually called from `run.rs`'s orchestration, or only from
  their own isolated tests? (Same standard applied to T2/T3 in v23/v24.)
- Run claim extraction and consensus scoring against a couple of real,
  already-grouped stories from a CI run and paste the actual output.

## P2 — Housekeeping

- The "39 binary tests" figure has been unresolved across four reports now —
  worth 10 minutes to paste the actual per-target `cargo test` header lines
  (`Running unittests src/lib.rs (...)` / `src/main.rs (...)`) and close it
  out for good, especially now that `main.rs`'s duplicate module declarations
  are a plausible explanation worth confirming or ruling out directly.
- `main.rs` still declares its own `mod` tree duplicating `lib.rs`'s modules
  rather than depending on the library crate — worth deduplicating while
  touching this file for the hang fix anyway, not a separate task.

---

## Cutover criteria (not started — this defines what "ready" means)

Once P0-P1 are done and verified with real pasted evidence:
- A full CI run (ingestion → grouping → gate → claims → consensus) completes
  without hangs or timeouts, against a scratch database, with real output at
  each stage.
- T4 and T5 are confirmed functionally correct on real data, not just
  compiling.

Only then: merge `rust-port` → `main`, repoint the production `.yml`
workflows at the Rust binary, add the Rust CI job to run on `main` pushes
(currently presumably still `rust-port`-scoped). This is its own task/PR,
same as always — don't fold it into anything above.