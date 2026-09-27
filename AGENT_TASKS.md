# AGENT_TASKS.md v26

Supersedes v25. V1 confirmed independently (CI run verified directly against
the GitHub Actions page, not just the report). W1/W2 hold. Infrastructure is
proven — the remaining work is making T5 functionally real, not just compiling.

## T5a — Wire rust-bert into extract_entities_top_n for real

- Replace the empty-dict placeholder with an actual call into rust-bert's NER
  pipeline.
- Confirm which checkpoint it loads and whether it preserves GPE vs LOC
  (flagged since v22) — run it against 5-10 real article bodies from the CI
  dry-run output and paste the actual entity output, not just "it compiles."

## T5b — Wire embeddings for real

- Same standard: actual model producing an actual vector for a real article,
  dimension confirmed (384 or whatever the chosen model outputs), not just a
  successful `cargo build`.

## T5c — Real extraction crate

- Still open since v24: replace `extract_from_html`'s regex tag-stripper with
  `trafilatura`, `readex`, `justext`, or `libreadability`. Run it against a
  couple of real URLs from the CI output and compare the extracted text
  against what the regex version produces — should visibly drop nav/ads/footer
  content.

## Housekeeping

- Paste raw `cargo test` output (unedited) once, to settle the 42-vs-39
  question definitively.
- Remove `candle` from `Cargo.toml` if T5a/b end up not using it.

Standard unchanged: "done" means real output from a real run, pasted as
evidence, not a compile success.