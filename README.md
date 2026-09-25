# News Pipeline

Daily news ingestion, verification, and curation pipeline for geopolitics and celebrity news. Built for zero-cost operation on GitHub Actions + Supabase/Neon.

## Architecture

```
GitHub Actions (cron) → Ingestion → Verification → Grouping → Gate → Curation UI
                              ↓
                        Supabase/Neon (PostgreSQL + pgvector)

Weekly (Sun 02:00 UTC):
  • Enrichment pipeline (media, video, snippets, embeddings)
  • Reliability snapshots (fact-check + consensus alignment)
  • Globe events backfill (geospatial from canonical entities)
```

## Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Compute** | GitHub Actions | Scheduled runs (6 AM / 6 PM UTC daily; Sun 02:00 UTC weekly), zero cost |
| **Database** | Supabase/Neon | PostgreSQL + pgvector, free tier |
| **LLM Primary** | Groq (`openai/gpt-oss-20b`) | Caption generation, classification, fact-checking, viewpoint clustering |
| **LLM Backup** | Cerebras (`gpt-oss-120b`) | 30-day trial fallback |
| **Ingestion** | RSS (BBC, Guardian, DW, France24, NPR, Al Jazeera, Euronews, PBS NewsHour) + GDELT (disabled) + Reddit | Tier-1 news, social; AP/Reuters via GDELT only (currently disabled) |
| **Verification** | MinHash containment | Near-dup clustering → reporting units |
| **Grouping** | Entity Jaccard (top-N, threshold 0.4) | Semantic story grouping |
| **Gate** | Tier-1 distinct owners ≥2 | Defamation-safe threshold |
| **Curation UI** | FastAPI + HTMX | Keyboard-driven triage (A/R/E) |
| **Enrichment** | Media, YouTube/Vimeo, Reddit/Twitter, LLM snippets, embeddings | Multimedia & semantic story enrichment |
| **Reliability** | Fact-checking (LLM + ClaimBuster) + Consensus alignment | Source trust scoring over time |
| **Globe/Events** | Canonical entities with lat/lon → EventGeometry | Geospatial event visualization |

## Quick Start

### 1. Prerequisites

- GitHub account
- Supabase or Neon account (free tier)
- Groq API key (free, no card)
- Python 3.12 and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### 2. Database Setup

1. Create a Supabase project (or Neon)
2. Enable `pgvector` extension in SQL editor: `CREATE EXTENSION IF NOT EXISTS vector;`
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

# Initialize database
uv run python -m scripts.init_db

# Test ingestion (dry run)
uv run python -m src.ingestion.run --dry-run

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
1. **Tier-1 RSS** (8 sources): BBC, Guardian, NPR, DW, France24, Al Jazeera, Euronews, PBS NewsHour
   - AP/Reuters removed 2026-09-21: 403/401 from GitHub runners
2. **Tier-2 RSS** (11 enabled): NYT, FT, Economist, Foreign Policy, Foreign Affairs, CSIS, WHO, LA Times, Chicago Tribune, Boston Globe, SFGate
   - 3 sources disabled (no working RSS): Brookings, Chatham House, UN
3. **GDELT DOC API**: Disabled (`GDELT_ENABLED=false`); code default enabled but redundant with RSS
4. **Reddit** (Tier-3): Top posts from r/worldnews, r/geopolitics, etc. (public `.rss` feeds, no credentials — anon-rate-limited, throttled to 1 subreddit/3s)

### Verification
1. **Extract**: trafilatura → body text
2. **Entities**: spaCy NER (PERSON, ORG, GPE top-N; N from `top_n_entities`, default 3)
3. **MinHash**: 5-shingles → 128-perm MinHash
4. **Cluster**: Direct containment (≥0.9) → Reporting Units
5. **Owner mapping**: Domain → ownership group (20 core groups)

### Grouping
- Cross-run 48h window + entity set Jaccard (≥0.4) → Stories
- Top-3 entity overlap handles multi-actor stories (sanctions, borders)

### Gate (Defamation-Safe)
- Story queued only if: **≥2 tier-1 reporting units** from **≥2 distinct owner groups**
- Single-source cascades (TMZ → 12 rewrites) blocked automatically
- All decisions logged for audit trail

### Curation
- FastAPI + HTMX UI at `localhost:8000`
- Keyboard: **A**pprove, **R**eject, **E**dit
- Draft captions via LLM (paraphrase-only constraint)
- Approved → `curated_posts` table, ready for manual posting

## Two-Week Protocol

**Do not automate posting yet.**

1. Run pipeline → triage in UI → manually post to your channels
2. Track engagement for 14 days
3. If hand-curated posts don't land, automation won't fix it
4. Then build scheduler + platform posters in week 3

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| GitHub Actions over VM | No idle-reclaim, no capacity queue, free minutes |
| Supabase/Neon over self-hosted | pgvector included, no patching, 7-day pause cleared by cron |
| Groq primary | No card, ongoing free tier, model deprecations handled via `.env` |
| MinHash direct (no LSH) | Daily bucket <50 articles → O(n²) is fine, avoids `MinHashLSHEnsemble` bug |
| Top-N entity Jaccard | Single top-1 fragments multi-actor stories |
| Tier-1 gate ≥2 distinct owners | Survives wire syndication (AP → 300 domains = 1 owner) |
| Paraphrase-only captions | Copyright compliance, not just defamation defense |
| Heartbeat commit | Keeps Actions schedule alive (60-day rule) |

## File Structure

```
.github/workflows/
  daily-ingest.yml          # Twice-daily ingestion pipeline
  weekly-enrichment.yml     # Weekly enrichment, reliability, globe backfill
  cleanup.yml               # DB cleanup (retention)
src/
  ingestion/                # RSS, GDELT, Reddit
  verification/             # Units, stories, tiers, viewpoint clustering
  reliability/              # Fact-checking, consensus analyzer, snapshots
  enrichment/               # Media, video, social snippets, LLM snippets, embeddings
  schema/models.py          # SQLAlchemy models
  shared/                   # Config, DB, LLM
  utils/                    # MinHash, NER, trafilatura
curation_ui/                # FastAPI + HTMX
scripts/                    # Init, seed, verify, backfill_globe_events
```

## Database Schema Changes

`init_db()` only creates missing tables; any change to a column or enum on an **EXISTING** table needs an idempotent SQL file in `supabase/migrations/` applied to Supabase **BEFORE** deploying. Run `uv run python scripts/check_schema.py` to detect drift; run `uv run python scripts/enable_rls.py` after new tables are created.

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
| Globe event layer | `supabase/migrations/` + `scripts/backfill_globe_events.py` |

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
- **Health**: `GET /healthz` on curation UI → stories by status, approved posts count
- **Alerts**: Exit code 1 if `total_fetched == 0` or tier-1 critical GDELT domains down; `::warning` per tier-1 RSS source with 0 ok articles

## Grouping Windows

Two separate windows serve different purposes:

| Stage | Window | Purpose |
|-------|--------|---------|
| Unit clustering (`build_reporting_units`) | Same UTC day | Cheap syndication detection: articles syndicated same day share exact text |
| Story attachment (`build_stories`) | 48 hours | Cross-run attachment: wire story breaking late Day 1, covered Day 2, same story |
