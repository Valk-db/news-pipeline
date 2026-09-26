# AGENT_TASKS.md v22

Supersedes v21 (v21 was never applied — this refines it, mainly T0/T5, before any
code exists, so nothing is lost). v20's paused P3 Python work (source_topic_reliability
schema etc.) is still paused, unchanged from v21 — revisit only if Tyler says so.

**Merge policy (standing, unchanged):** once your own checks pass for a task —
`cargo build` clean, targeted `cargo test` green — merge it yourself, separate PR
per task. Don't wait for review. Flag any scope change or substituted approach in
the PR description — don't fold it in silently, same convention as the Python
AGENT_TASKS history.

**Branch:** all of this happens on a new `rust-port` branch off `main`. Each task
below is its own PR **into `rust-port`**, self-merged per the policy above — not
into `main`. `main` keeps running the live Python pipeline via the existing
`.yml` workflows untouched until the Cutover task at the end explicitly merges
`rust-port` → `main`. If a task's PR sits half-broken, `main` is never affected.

---

## Scope

Target: replace the pipeline backend (`src/`, `scripts/`, the cron jobs in
`.github/workflows/daily-ingest.yml`, `weekly-enrichment.yml`, `cleanup.yml`)
with a Rust binary. Same Supabase Postgres, same $0/month budget, same GitHub
Actions compute.

**Not in scope:** `curation_ui/` stays Python/FastAPI on Vercel — no mature
general-purpose Rust web-app runtime there, and the UI already just reads/writes
the same tables regardless of what language wrote them.

**Where the Rust code lives:** `pipeline-rs/` at repo root, not root itself —
keeps it additive alongside the existing `pyproject.toml`/`uv.lock` packaging
and the still-running Python CI, until Cutover.

---

## Infra — Supabase connection strategy (read before T1)

1. **Use Supavisor session mode (port 5432), not direct connection, not
   transaction mode (port 6543).**
   - GitHub-hosted runners have no outbound IPv6 by default; Supabase's raw
     direct-connection string is IPv6-only without the paid IPv4 add-on.
   - sqlx's Postgres driver uses protocol-level named prepared statements for
     nearly every query — Supavisor's transaction-mode pooler doesn't support
     those. This is a known, structural incompatibility, not something to
     debug around.
   - Session mode (`aws-0-<region>.pooler.supabase.com:5432`) is IPv4-safe and
     behaves like a normal direct connection to sqlx. Pull the exact URI from
     Database Settings → Connection string (pooling display on, port 5432) —
     don't hand-construct it, the host is region-specific.
2. **Keep `max_connections` low** — `PgPoolOptions::new().max_connections(3)`
   to start. A GitHub Actions run is one batch process, not a concurrent server;
   check the project's Pool Size in Database Settings and stay well under it,
   since `curation_ui`'s own `asyncpg` pool shares the same cap.
3. Require TLS; let the pool close naturally at the end of `main()`.
4. `runtime-tokio-native-tls` (as asked) is fine to start with; `tls-rustls` is
   a drop-in swap if OpenSSL/pkg-config gives the Actions build any trouble —
   note it, don't switch silently.
5. Use `cargo add sqlx --features postgres,runtime-tokio-native-tls,chrono,uuid`
   rather than hand-typing `Cargo.toml` — `cargo add` errors immediately on an
   unknown feature name instead of silently accepting a bad list, and sqlx's
   feature names have shifted across versions.

---

## T1 — Scaffolding + shared layer

- `cd pipeline-rs && cargo init --bin`
- `cargo add tokio --features full`
- `cargo add serde --features derive`
- `cargo add serde_json reqwest --features reqwest/json`
- `cargo add dotenvy`
- Port `src/shared/config.py` → `config.rs`: same env var names (`database_url`,
  `groq_api_key`, `groq_model`, `cerebras_api_key`, `cerebras_model`,
  `reddit_user_agent`, `rss_fetch_timeout`, `max_articles_per_feed`,
  `groq_daily_request_budget`, `containment_threshold`,
  `min_reporting_units_per_story`, `top_n_entities`, etc.) — no secret renaming
  in GitHub Actions.
- Port `src/shared/database.py` → `database.rs`: `PgPool` per the Infra section.
- Port `src/shared/llm.py` + `llm_budget.py` → `llm.rs`: Groq and Cerebras are
  both OpenAI-compatible chat-completions endpoints — plain `reqwest` +
  `serde_json`, no SDK.
- Port `src/schema/models.py` → `models.rs`: structs with `#[derive(sqlx::FromRow)]`
  matching `supabase/migrations/`.
- **Done when:** `cargo build` succeeds, `main()` opens the pool and runs
  `SELECT 1`.

## T2 — Ingestion + mechanical extraction bits

- `src/ingestion/rss.py` → `feed-rs` crate for RSS/Atom.
- `src/ingestion/reddit.py`, `adapter.py`, `source_registry.py`,
  `tiered_scheduler.py`, `run.py` → HTTP + orchestration, translates directly.
- `src/ingestion/gdelt.py` — disabled currently, port last or skip.
- **From `trafilatura_extract.py`:** `canonicalize_url` and the tracking-param
  stripping (`_TRACKING_PARAMS`/`_TRACKING_PREFIXES`) are pure string
  manipulation, no library dependency — port now, doesn't wait on T5.
- Run `cargo check` after each file, view the error, fix, move on.

## T3 — Verification arithmetic + entity canonicalization + utils

- `src/utils/minhash_utils.py`, `ingest_stats.py` → portable, no ML.
- `src/verification/tiers.py`, `topics.py`, `cleanup.py` → portable.
- **From `ner.py`:** everything *except* the spaCy call is portable now —
  `_normalize_text`, `_generate_aliases`, `EntityCanonicalizer`,
  `resolve_entities_to_canonical`, `canonical_jaccard`. This is regex +
  Postgres reads (currently SQLAlchemy against `CanonicalEntity`/`EntityAlias`),
  no model dependency — port it here, against T1's `models.rs`/`database.rs`.
- `src/verification/units.py`, `stories.py` → port the entity-Jaccard grouping
  math to accept an already-extracted `Dict<label, Vec<String>>` as input
  (matching `extract_entities_top_n`'s return shape) so it isn't blocked on T5.

## T4 — LLM-calling verification/reliability

- `src/verification/claims.py`, `narrative.py`,
  `src/reliability/consensus_analyzer.py`, `fact_checker.py` → request-building
  + response-parsing against T1's `llm.rs`, portable once that lands.

---

## T5 — NER, embeddings, extraction: wire against real crates, verify, don't invent

The only genuinely ML-shaped pieces left. For each: build it against the crate
below, then validate on real data before calling it done — this is "swap the
model runtime and confirm the output still means the same thing downstream,"
not "translate the file."

1. **NER** — only `extract_entities_top_n` itself (the spaCy call), everything
   else moved to T3 above. `rust-bert` (Hugging Face Transformers port, tch or
   onnxruntime backend) ships a ready `NERModel`/`TokenClassificationModel`.
   **The catch:** spaCy's `en_core_web_sm` uses the OntoNotes label set —
   PERSON, ORG, GPE, LOC, EVENT, PRODUCT — and `get_primary_entity_set`
   specifically needs the GPE-vs-LOC distinction. Most off-the-shelf BERT NER
   checkpoints (including common `rust-bert` defaults) are CoNLL-2003-trained
   (PER/ORG/LOC/MISC), which collapses GPE into LOC and drops EVENT/PRODUCT
   entirely. Before wiring this in: confirm which checkpoint `rust-bert`'s NER
   pipeline actually loads, and whether it (or another onnx-loadable checkpoint)
   preserves an OntoNotes-style split. If nothing clean does, that's a scoped,
   upstream-able gap — not a reason to bail on `rust-bert` for this task.
2. **Embeddings** — currently `sentence-transformers/all-MiniLM-L6-v2`, 384
   dims, feeding pgvector. Check whether `rust-bert`'s sentence-embeddings
   pipeline has this exact preset. The Python code already has an
   API-fallback path scaffolded (`use_local=False` + `api_key`, currently
   unused) — mirror that same optionality in Rust rather than forcing local-only.
   Whatever model gets picked: re-embed a sample of existing rows and diff
   nearest-neighbor results against current pgvector data before calling this
   done — a different model produces different vectors, not just a different
   runtime.
3. **Extraction** — only the `trafilatura.extract(...)` call itself, the
   URL-canonicalization already moved to T2. Candidates now exist:
   `trafilatura` crate itself (Rust port via an intermediate Go port), `justext`
   (paragraph-level, has published parity benchmarks vs. Python), `libreadability`,
   or `readex` (runs a Readability → Trafilatura → htmldate cascade with
   differential parity testing built in — closest in spirit to what
   `trafilatura` does internally). These are all young/low-version crates —
   spot-check the pick against a sample of real articles from your actual RSS
   sources before trusting it, more scrutiny than `rust-bert` gets, not less.

For all three: if wiring one up surfaces a specific gap — a missing model
preset, a label-scheme mismatch, a site your extraction crate mishandles that
trafilatura didn't — that gap is the scoped, upstream-able contribution. Write
it up in the PR description, same convention as everything else here. Don't
route around it silently.

---

## Cutover (own task, own PR, at the end)

Once T1–T5 have parity with Python (tests green, a real run against a scratch
Supabase project produces the same shape of output) — merge `rust-port` into
`main`, repoint `daily-ingest.yml` / `weekly-enrichment.yml` / `cleanup.yml` at
the compiled binary, add a `cargo build --release` + `cargo test` step to
`ci.yml`. Not folded into any task above.