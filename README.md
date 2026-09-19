# News Pipeline

Daily news ingestion, verification, and curation pipeline for geopolitics and celebrity news. Built for zero-cost operation on GitHub Actions + Supabase/Neon.

## Architecture

```
GitHub Actions (cron) → Ingestion → Verification → Grouping → Gate → Curation UI
                              ↓
                        Supabase/Neon (PostgreSQL + pgvector)
```

## Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Compute** | GitHub Actions | Scheduled runs (6 AM / 6 PM UTC), zero cost |
| **Database** | Supabase/Neon | PostgreSQL + pgvector, free tier |
| **LLM Primary** | Groq (`openai/gpt-oss-20b`) | Caption generation, classification |
| **LLM Backup** | Cerebras (`gpt-oss-120b`) | 30-day trial fallback |
| **Ingestion** | RSS + GDELT + Reddit | Tier-1 news, wires, social |
| **Verification** | MinHash containment | Near-dup clustering → reporting units |
| **Grouping** | Entity Jaccard (top-3) | Semantic story grouping |
| **Gate** | Tier-1 distinct owners ≥2 | Defamation-safe threshold |
| **Curation UI** | FastAPI + HTMX | Keyboard-driven triage (A/R/E) |

## Quick Start

### 1. Prerequisites

- GitHub account
- Supabase or Neon account (free tier)
- Groq API key (free, no card)
- Reddit API credentials
- Python 3.11+ and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

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
| `REDDIT_CLIENT_ID` | reddit.com/prefs/apps |
| `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps |
| `YOUTUBE_API_KEY` | Google Cloud Console (optional) |

### 4. Local Development

```bash
# Clone and install
git clone <your-repo>
cd news-pipeline
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your keys

# Initialize database
uv run scripts/init_db.py

# Verify tier-1 sources
uv run scripts/verify_sources.py

# Test ingestion (dry run)
uv run src/ingestion/run.py --dry-run

# Start curation UI
uv run curation_ui/main.py
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
1. **RSS**: BBC, Guardian, NPR, Google News World
2. **GDELT DOC API**: AP, Reuters, BBC, Guardian, NPR (domain-filtered, 5s throttle)
3. **Reddit**: Top posts from r/worldnews, r/geopolitics, etc. (PRAW, 100 QPM)

### Verification
1. **Extract**: trafilatura → body text
2. **Entities**: spaCy NER (PERSON, ORG, GPE top-3)
3. **MinHash**: 5-shingles → 128-perm MinHash
4. **Cluster**: Direct containment (≥0.9) → Reporting Units
5. **Owner mapping**: Domain → ownership group (20 core groups)

### Grouping
- Same UTC day + entity set Jaccard (≥0.3) → Stories
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
| Status log commit | Keeps Actions schedule alive (60-day rule) |

## File Structure

```
.github/workflows/daily-ingest.yml   # Cron pipeline
src/
  ingestion/                         # RSS, GDELT, Reddit
  verification/                      # Units, stories, tiers
  schema/models.py                   # SQLAlchemy models
  shared/                            # Config, DB, LLM
  utils/                             # MinHash, NER, trafilatura
curation_ui/                         # FastAPI + HTMX
scripts/                             # Init, seed, verify
```

## Extending

| Need | Where to Add |
|------|--------------|
| New RSS feed | `src/ingestion/rss.py` → `TIER1_FEEDS` |
| New GDELT domain | `src/ingestion/gdelt.py` → `DOMAIN_FILTERS` |
| New ownership group | `src/verification/units.py` → `OWNERSHIP_GROUPS` |
| New tier-1 source | `src/verification/tiers.py` → `TIER1_DOMAINS` |
| New LLM model | `.env` → `GROQ_MODEL` (no code change) |
| Celebrity vertical | Week 3: add `tier2_gate` in `tiers.py` |

## Monitoring

- **Actions tab**: Run history, logs
- **Status logs**: `logs/status_*.json` (committed each run, 30-day retention)
- **Database**: Query `stories` table for gate pass/block rates

## Cost

**$0/month** on free tiers:
- GitHub Actions: 2,000 min/mo (private) / unlimited (public)
- Supabase: 500 MB, pgvector included
- Groq: 1K req/day, 200K tokens/day
- Cerebras: 30-day trial (optional)
