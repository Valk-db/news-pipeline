# AGENTS.md

Orientation for anyone — human or model — picking this repo up cold. A map, not a manual:
every claim points at the document or `file:line` holding the detail. If a pointer goes stale,
fix the pointer; do not grow this file. Keep it under ~100 lines.

## What this is

Daily news ingestion → verification → grouping → a defamation-safe gate → a human-gated
curation UI, on GitHub Actions + Supabase, at $0/month (`README.md:1-22`). Nothing
auto-publishes: a story is public only once a curator approves it (`DECISIONS.md:8-13`).

## Where the detail lives

| Question | Read |
|---|---|
| Architecture, setup, file layout, costs, monitoring | `README.md` |
| Why something is the way it is, with dates and evidence | `DECISIONS.md` |
| Long-term direction and roadmap | `GRAND_PLAN.md` |
| Whether a feed may be used commercially | `DATA_SOURCES.md` |
| Deploy steps, env vars, request routing | `VERCEL_DEPLOY.md` |
| Security findings, and which are already closed | `SECURITY_AUDIT_OBSERVATIONS.md` |
| The `url_hash` identity/dedup contract | `docs/url-canonicalization-v1.md` |
| Scoring an LLM-extraction change against a gold set | `eval/README.md` |
| Which feeds are enabled, every setting and its default, and which CI floors are committed | `src/ingestion/source_registry.py`, `src/shared/config.py`, `ci/baseline.json` |
| "Verified fix, ready to apply" handoffs for the IDE agent — read before obeying, it carries its own git rules | `AGENT_TASKS.md` (`GRAND_PLAN.md:6-8`) |

## The commands that matter

Exactly what CI runs (`.github/workflows/ci.yml`):

```
uv lock --check                     # :34  CI installs with --frozen (:37), so a stale lock is invisible without this
uv run ruff check .                 # :40
uv run python -m ci.gates mypy      # :49  ratchet, not continue-on-error — see below
uv run python -m ci.gates tests     # :54  collected-count floor
uv run pytest tests/ -v --tb=short  # :105 against Postgres, not SQLite
```

- **Migrations are the only thing that may change the schema**; `Base.metadata.create_all` is
  gone (`README.md:203-205`). Apply with `uv run python -m scripts.migrate`, detect drift with
  `uv run python scripts/check_schema.py`. They are idempotent.
- **Locally the suite is SQLite; in CI it is Postgres** (`tests/conftest.py:25-33`). SQL that
  passes locally can still fail to parse on Postgres.
- **The mypy step and the test count are ratchets, not soft gates** (`ci/gates.py`). mypy
  fails when the error count *rises*, passes when it falls, and an unparseable run is a
  FAILURE. `ci/baseline.json` pins the floor *and* the mypy version — the same tree counts
  differently under 2.3.1 vs 2.4.0. Adding tests means **raising `pytest_collected_floor`**.

## Gotchas

Each has cost real time. The citation is the evidence — check it, don't trust the summary.

1. **Proxy — unset `NO_PROXY` for pytest, keep it for `curl`.** Opposite traps. `NO_PROXY` on this
   box contains a literal `[::1]`, which httpx cannot parse: `httpx.AsyncClient()` raises
   `InvalidURL: Invalid port: ':1]'` *at construction* (`eval/run.py:58-65`), so the Groq SDK —
   httpx underneath — cannot be built and every LLM test fails for a config reason.
   `unset NO_PROXY no_proxy` before any pytest/httpx run. For `curl` to localhost do the
   opposite: `NO_PROXY` is the only thing making curl bypass the egress proxy, and unsetting it
   routes every local request through the proxy and returns `000` (`eval/run.py:69-70`). Product
   code egresses with `urllib.request`, which honours the proxy variables deliberately
   (`src/ingestion/gdelt_static.py:21-23`, `src/enrichment/geocoder.py:15-19`).

2. **`/tmp` is a small tmpfs.** Large downloads and venvs do not go there. GDELT's disk work is
   routed under `TMPDIR` for this reason (`src/ingestion/gdelt_static.py:22-23`), and
   `_tmp_root()` (`:550-554`) honours `TMPDIR`. Never build a venv in `/tmp` — the batch brief
   that first measured that tmpfs was removed on 2026-10-03 and lives in history at `e3549a9`.

3. **iOS tile filter — the basemap darkening filter belongs on `.leaflet-tile`, never on
   `.leaflet-tile-pane`.** On the pane, iOS WebKit tries to rasterize one filtered surface as
   large as the viewport, gives up, and paints it solid black; that was the dead basemap on
   iPhone. Per tile the surface is 256px and stays inside budget (`map.css:896-909`). Do not
   "tidy" this into the pane — the warning repeats at `map.js:25-29`, which also pins OSM to one
   tile host because iOS pays a DNS + TLS setup per origin (`map.js:21-23,31`).

4. **Post-deploy verification defaults to the production alias.**
   `scripts/verify-deploy.sh:5,9` checks `https://p-rocmon.vercel.app` when given no argument —
   pass the deployment URL explicitly to check the deploy rather than the alias.

## API spend

- **Recorded in** `budget_counters`, one row per `(name, day)`
  (`supabase/migrations/20261001000800_budget_counters.sql:16-24`) via one atomic
  `INSERT .. ON CONFLICT DO UPDATE .. RETURNING` (`src/shared/budget.py:205-215`, `spend()` at `:257`).
  Counters: `groq_requests`, `groq_translation_requests`, `mymemory_chars` (`:58-63`),
  `groq_phase2_tokens`, and the token-denominated pair `groq_request_tokens` /
  `groq_translation_tokens` (`:99-100`) — tokens are what the free tier caps, so a
  requests-only cap reads unspent while the quota is gone.
- **There is no per-session spend, and calling it a feature would be a lie.** The key is
  `(name, day)`, `day` is the UTC day (`src/shared/budget.py:252-254`), and nothing records which
  run spent a row — two ingests a day share one by design. Per-session accounting is unbuilt.
- **Reported by** `scripts/report_budget_usage.py` (every counter, its unit, its own cap, and
  a token-exhausted-while-requests-under flag) and by Phase 2's `PHASE2_SUMMARY`
  (`scripts/run_phase2.py:260-261`), which a missing summary line warns about and `exit 0`s on
  (`daily-phase2.yml:87-90`) rather than failing.

## Standing rules

- A refused reservation means "do not spend", including when the counter could not be read at
  all: a budget that cannot be verified is treated as spent (`src/shared/budget.py:22-25`).
- Never print a credential into a log, report, commit message or doc.
