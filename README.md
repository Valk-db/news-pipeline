# News Pipeline

Daily news ingestion, verification, and curation pipeline for geopolitics and celebrity news. Built for zero-cost operation on GitHub Actions + Supabase/Neon.

## Architecture

```
GitHub Actions (cron) → Ingestion → Verification → Grouping → Gate → Curation UI
                              ↓
                        Supabase/Neon (PostgreSQL)

Weekly (Sun 02:23 UTC):
  • Enrichment pipeline (media, video, snippets, embeddings)
  • Reliability snapshots (fact-check + consensus alignment)
  • Map events backfill (geospatial from canonical entities)
```

## Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Compute** | GitHub Actions | Scheduled runs (06:23 and 18:23 UTC daily; Sun 02:23 UTC weekly), zero cost |
| **Database** | Supabase/Neon | PostgreSQL, free tier (pgvector installed; no vector column in use yet) |
| **LLM Primary** | Groq (`openai/gpt-oss-20b`) | Claim extraction, fact-checking, viewpoint clustering, LLM snippets |
| **LLM Backup** | Cerebras (`gpt-oss-120b`) | 30-day trial fallback |
| **Ingestion** | RSS tier-1 (8 enabled of 10 configured) + tier-2 (13 enabled of 23) + Reddit + sensors, GDELT disabled in the workflow | Tier-1 news, social, hazard feeds; AP/Reuters configured but disabled (no working feed) |
| **Verification** | MinHash containment | Near-dup clustering → reporting units |
| **Grouping** | Entity Jaccard (top-N, threshold 0.4) | Semantic story grouping |
| **Gate** | ≥2 tier-1 articles from ≥2 distinct owner groups | Defamation-safe threshold |
| **Curation UI** | FastAPI + HTMX | Read-only queue: the filtered story list and a detail view per story |
| **Enrichment** | Media, YouTube/Vimeo, Reddit/Twitter, LLM snippets, embeddings | Multimedia & semantic story enrichment |
| **Reliability** | Fact-checking (LLM + ClaimBuster) + Consensus alignment | Source trust scoring over time |
| **Map/Events** | Canonical entities with lat/lon → EventGeometry | Geospatial event data behind `/map` |

## Quick Start

### 1. Prerequisites

- GitHub account
- Supabase or Neon account (free tier)
- Groq API key (free, no card)
- Python 3.12 and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 2. Database Setup

1. Create a Supabase project (or Neon)
2. Apply the schema: `uv run python -m scripts.migrate` (or paste `supabase/migrations/*.sql` into
   the SQL editor). Embeddings are still JSON array columns, so nothing needs a vector column;
   the pgvector extension itself is installed by `20261002220200_pgvector_readiness.sql` and
   nothing queries it yet
3. Copy connection string → GitHub secret `DATABASE_URL`

### 3. Secrets (GitHub → Settings → Secrets → Actions)

| Secret | Source |
|--------|--------|
| `DATABASE_URL` | Supabase/Neon connection string |
| `GROQ_API_KEY` | console.groq.com |
| `CEREBRAS_API_KEY` | cerebras.ai (optional, 30-day trial) |
| `YOUTUBE_API_KEY` | Google Cloud Console (optional) |

### 4. Local Development

```bash
# Clone and install
git clone <your-repo>
cd news-pipeline
uv sync --extra pipeline

# Configure environment
cp .env.example .env
# Edit .env with your keys

# Create or update the schema from supabase/migrations/
uv run python -m scripts.migrate

# Test ingestion (dry run)
uv run python -m src.ingestion.run --dry-run

# Dev runs (`--env dev` loads .env.dev over .env, and in CI
# `.github/workflows/daily-ingest.yml` sets them when target_db=dev):
# RSS_FETCH_CONCURRENCY=25 and RSS_FETCH_TIMEOUT=15. Production keeps the
# defaults 10 / 30s.

# Start curation UI
uv run uvicorn curation_ui.main:app --reload
# Open http://localhost:8000
```

### 5. Remote UI Access (Free)

```bash
# Option 1: Tailscale (recommended)
tailscale up  # on desktop
tailscale up  # on phone/laptop
# Access via http://<desktop-tailscale-ip>:8000

# Option 2: Cloudflare Tunnel (testing only)
cloudflared tunnel --url http://localhost:8000
# → https://<random>.trycloudflare.com
```

## Pipeline Flow

### Ingestion (Twice Daily)
1. **Tier-1 RSS** (8 enabled of 10 configured): BBC, Guardian, NPR (4 feeds), DW, France24, Al Jazeera, Euronews, PBS NewsHour
   - AP/Reuters configured but `enabled=False` (verified 2026-10-01: AP URLs are HTML hub pages, Reuters returns HTTP 401)
2. **Tier-2 RSS** (13 enabled of 23 configured): Foreign Policy, Foreign Affairs, CSIS, WHO, UN, AllAfrica, RTE, Middle East Eye, ReliefWeb, and others
   - Disabled after live-run evidence: NYT, WaPo, WSJ, FT, Economist, LA Times, Chicago Tribune, Boston Globe
   - Also disabled: SCMP, The New Humanitarian
   - Brookings and Chatham House are commented out in the registry (no working feed)
3. **GDELT DOC API**: Disabled (`GDELT_ENABLED=false`); code default enabled but redundant with RSS
4. **Reddit** (Tier-3): Top posts from r/worldnews, r/geopolitics, etc. (public `.rss` feeds, no credentials — anon-rate-limited, throttled to 1 subreddit/3s)
5. **Sensors** (Tier-3): USGS earthquakes (~290 features/day) + GDACS alerts (~223 items), fetched by `src/ingestion/sensors.py`; both have no RSS URLs and are kept out of the RSS sweep
6. **RSS evidence locker** (opt-in): 4 hand-verified tier-1 feeds, full article body + content hash, one Merkle-log append per article, max 10 bodies per cycle. Selected with `--sources rss_evidence`; not part of a default run
7. **Bluesky**: no article ingestion. `bsky.social` is only a tier-3 domain in the registry; posts arrive as enrichment snippets through the AT Protocol API, and only when `BLUESKY_HANDLE`/`BLUESKY_PASSWORD` are set

See `src/ingestion/source_registry.py` for the complete, up-to-date source registry with all RSS URLs, tiers, and enable/disable status.

### Verification
1. **Extract**: trafilatura → body text
2. **Entities**: spaCy NER (PERSON, ORG, GPE top-N; N from `top_n_entities`, default 3)
3. **MinHash**: 5-shingles → 128-perm MinHash
4. **Cluster**: Direct containment (≥0.9) → Reporting Units
5. **Owner mapping**: Domain → ownership group (123 domains → 118 groups)

### Grouping
- Cross-run 48h window + entity set Jaccard (≥0.4) → Stories
- Top-3 entity overlap handles multi-actor stories (sanctions, borders)

### Gate (Defamation-Safe)
- Story queued only if: **≥2 tier-1 articles** from **≥2 distinct owner groups**
- The count is over articles, not reporting units: `tier1_owner_groups` is an article count per
  owner and `evaluate_tier1_gate` expands it, so one unit holding BBC and Guardian copies of the
  same wire story passes on its own. Whether that is the intended corroboration bar is an open
  decision (`AGENT_TASKS_v38.md` §"Blocked on Tyler", item 1)
- Single-source cascades (one owner → many rewrites) blocked automatically
- All decisions logged for audit trail

### Phase 2 Enrichment (Daily)
- Runs after both ingests (`.github/workflows/daily-phase2.yml`, `23 20 * * *`), on
  gate-passed `PENDING` stories plus any already-`QUEUED` ones
- Adds topic groups → narrative arcs → claims, each step skipping stories whose
  derived state is already current (so a second run costs nothing)
- Excludes viewpoint children, and any story with fewer than 2 text-bearing units
- **Changes no story's status.** Public exposure stays human-gated; see `DECISIONS.md`
- Budgeted in **tokens** on its own counter (`groq_phase2_tokens`, 40,000/day = 20%
  of Groq's 200,000 TPD) because the daily token cap binds long before the request cap

### Curation
- FastAPI + HTMX UI at `localhost:8000`
- **Read-only.** The queue (`/`) and the per-story detail view (`/story/{story_id}`) are
  both behind `require_auth` and neither mutates a row. Approve, reject, edit and save were
  removed on 2026-10-02 (`tests/test_route_table.py` pins that they are gone, not just unlinked)
- A story leaves `PENDING` through the gate and the cleanup job, never through a button
- Nothing in the app writes a `curated_posts` row; the table and its model remain in the schema
  but nothing reads or writes them, and `curation_ui/health.py` no longer counts them either

## Two-Week Protocol

**Do not automate posting yet.**

1. Run pipeline → read the queue and the map → post by hand, outside this app
2. Track engagement for 14 days
3. If hand-curated posts don't land, automation won't fix it
4. Then build scheduler + platform posters in week 3

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| GitHub Actions over VM | No idle-reclaim, no capacity queue, free minutes |
| Supabase/Neon over self-hosted | pgvector installed and ready for the vector column a later batch may add, no patching, 7-day pause cleared by cron |
| Groq primary | No card, ongoing free tier, model deprecations handled via `.env` |
| MinHash direct (no LSH) | Daily bucket <50 articles → O(n²) is fine, avoids `MinHashLSHEnsemble` bug |
| Top-N entity Jaccard | Single top-1 fragments multi-actor stories |
| Tier-1 gate ≥2 distinct owners | Survives wire syndication (AP → 300 domains = 1 owner) |
| Paraphrase-only captions | Copyright compliance, not just defamation defense. Historical: the caption path died with the approve/edit flow on 2026-10-02 and nothing calls `build_deterministic_caption` or `validate_caption` any more |
| Heartbeat commit | Keeps Actions schedule alive (60-day rule) |
| Phase 2 enriches `PENDING`, never publishes | Public exposure stays human-gated; `DECISIONS.md` holds the evidence and the pre-registered exit criteria |

## File Structure

```
.github/workflows/
  daily-ingest.yml          # Twice-daily ingestion pipeline
  weekly-enrichment.yml     # Weekly enrichment, reliability, event backfill
  cleanup.yml               # Weekly scripts/cleanup_stale.py: expire stale PENDING/BLOCKED
                            # stories, drop orphaned reporting units and stale links. The
                            # retention job (scripts/run_retention.py: drop old embeddings,
                            # NULL old body_text) is manual — no workflow runs it.
  ci.yml                    # ruff, mypy, pytest against a Postgres service
src/
  ingestion/                # RSS, GDELT, Reddit, sensors, RSS evidence locker
  verification/             # Units, stories, tiers, viewpoint clustering, cleanup, retention
  reliability/              # Fact-checking, consensus analyzer, snapshots
  enrichment/               # Media, video, social snippets, LLM snippets, embeddings
  transparency/             # Signed Merkle log, checkpoints, inclusion proofs, anchoring
                           # (appends come from the opt-in RSS evidence locker; the table
                           # exists in supabase/migrations/20261001000700_merkle_log_entries.sql)
  schema/models.py          # SQLAlchemy models
  shared/                   # Config, DB, LLM, budget
  utils/                    # MinHash, NER, trafilatura
curation_ui/                # FastAPI + HTMX (main, security, health)
scripts/                    # Migrate, schema/RLS/freshness/orphan checks, cleanup, retention,
                            #   backfills, topic/claim/reliability jobs
```

## Database Schema Changes

Every schema change — **new tables**, new columns, column type changes, enum type names and labels — is an idempotent SQL file in `supabase/migrations/`, and `supabase/migrations/` is the only thing that changes the schema. `Base.metadata.create_all` is gone: it used to run from `init_db()` on every ingest and weekly job, which is how dev ended up with SQLAlchemy's enum type names while the migration files declared different ones, and why five tables the models expect had never been created. Apply with `uv run python -m scripts.migrate` (or paste the files into the Supabase SQL editor, which is the production path). Run `uv run python scripts/check_schema.py` to detect drift, and `uv run python scripts/enable_rls.py` to (re-)assert row level security: the migrations already `ENABLE ROW LEVEL SECURITY` on every table they create — 34 named in static statements, plus `20261002220000_row_level_security_backfill.sql`, which loops over anything still missing it — and create no policies, and the script repeats that statement for the 30 tables `Base.metadata` declares, then warns if any of them has a policy — the condition that would reopen the anonymous REST path.

## Daily Budgets

Two free quotas are spent against a row in `budget_counters` (`name`, `day`, `used`), not
against anything held in memory or on disk: Groq requests (`groq_daily_request_budget`,
900) and MyMemory translation characters (45,000, under the anonymous 50,000 limit).

Phase 2 is counted separately and in **tokens** (`groq_phase2_tokens`,
`phase2_daily_token_cap` 40,000). Requests are the wrong unit: on 2026-10-03 dev held 16
Groq requests against a fully spent 200,000-token day, so a request budget would have
read 96% unspent while the quota was gone. See `DECISIONS.md`.

One statement reserves and counts, so the cap holds across processes — two ingest runs a
day share one budget instead of each getting a full one, which is what an in-process
counter or a file on an ephemeral runner meant in practice. A reservation is refused
rather than allowed when the cap is reached **or** when the counter cannot be read: an
unverifiable budget is treated as spent, because the quota is the thing that cannot be
replenished. See `src/shared/budget.py`.

## Extending

| Need | Where to Add |
|------|--------------|
| New RSS feed (any tier) | `src/ingestion/source_registry.py` → `TIER1_SOURCES` / `TIER2_SOURCES` / `TIER3_SOURCES` / `TIER4_SOURCES` |
| New GDELT domain | `src/ingestion/gdelt.py` → `DOMAIN_FILTERS` |
| New ownership group | `src/verification/units.py` → `OWNERSHIP_GROUPS` |
| New tier-1 source | `src/verification/tiers.py` → `TIER1_DOMAINS` |
| New LLM model | `.env` → `GROQ_MODEL` (no code change) |
| Celebrity vertical | Week 3: add `tier2_gate` in `tiers.py` |
| Enrichment provider | `src/enrichment/` (media_extractor, video_finder, social_snippets, snippet_extractor) |
| Reliability fact-checker | `src/reliability/fact_checker.py` (ClaimBuster, custom APIs) |
| Map event layer | `supabase/migrations/` + `scripts/backfill_globe_events.py` |

## Cost

**$0/month** on free tiers:
- GitHub Actions: 2,000 min/mo (private) / unlimited (public)
- Supabase: 500 MB, pgvector included
- Groq: 1K req/day, 200K tokens/day
- Cerebras: 30-day trial (optional)
- GDELT: Disabled (reduces Actions minutes and API pressure)

## Monitoring

- **GitHub Actions**: `PYTHONUNBUFFERED=1` for real-time logs; `GDELT_ENABLED=false` to cut noise
- **Run summary**: Ingestion stats table (fetched/too_short/ok/failed per source) in workflow step summary
- **Health**: `GET /healthz` on curation UI → `{"status", "database"}` only, always 200 (a DB blip
  reports `degraded` in the body rather than failing the probe). The diagnosis behind it —
  Python version, which env vars are set, DB topology, stories by status and transparency
  checkpoint freshness — is on `GET /healthz/details`, which requires the curator credentials.
  Its verdict is derived from transparency alone (`curation_ui/health.py`)
- **Alerts**: exit code 1 if `total_fetched == 0`, if ≥50% of enabled tier-1 RSS sources are
  broken or none of them produced anything, or if `scripts/check_freshness.py` finds no tier-1
  article in 30h. Each broken tier-1 source also gets an `::error` annotation, and
  `scripts/check_orphans.py` fails the run when its report is incomplete. (`run.py` also has a
  "tier-1 critical GDELT domains down" exit, but   `GDELT_TIER1_CRITICAL_DOMAINS` is empty, so that branch cannot fire at all.)

## Grouping Windows

Two separate windows serve different purposes:

| Stage | Window | Purpose |
|-------|--------|---------|
| Unit clustering (`build_reporting_units`) | Same UTC day | Cheap syndication detection: articles syndicated same day share exact text |
| Story attachment (`build_stories`) | 48 hours | Cross-run attachment: wire story breaking late Day 1, covered Day 2, same story |
