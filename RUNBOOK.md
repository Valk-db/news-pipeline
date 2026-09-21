# News Pipeline Operational Runbook

## Overview
This runbook covers the daily ingestion pipeline, curation UI, and common operational tasks.

---

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│   GDELT     │    │    RSS       │    │   Reddit     │
│   API       │───▶│    Feeds     │───▶│   API        │
└─────────────┘    └──────────────┘    └──────────────┘
       │                  │                  │
       └──────────────────┼──────────────────┘
                          ▼
              ┌───────────────────────┐
              │   Ingestion Pipeline  │
              │  (src/ingestion/run)  │
              └───────────┬───────────┘
                          ▼
              ┌───────────────────────┐
              │   Verification        │
              │  (units → stories)    │
              └───────────┬───────────┘
                          ▼
              ┌───────────────────────┐
              │   Tier-1 Gate         │
              │  (≥2 tier-1 distinct  │
              │   ownership groups)   │
              └───────────┬───────────┘
                          ▼
              ┌───────────────────────┐
              │   Curation UI         │
              │  (FastAPI + HTMX)     │
              └───────────┬───────────┘
                          ▼
              ┌───────────────────────┐
              │   Curated Posts       │
              │  (APPROVED → POSTED)  │
              └───────────────────────┘
```

---

## Environment Variables

### Required
- `DATABASE_URL` - PostgreSQL connection string (required for all operations)
- `GROQ_API_KEY` or `CEREBRAS_API_KEY` - LLM for caption generation

### Ingestion Controls
- `GDELT_ENABLED` - Enable/disable GDELT ingestion (default: `true`)
- `GDELT_THROTTLE_SECONDS` - Rate limit between GDELT domain requests (default: 5.0)
- `RSS_FETCH_TIMEOUT` - Timeout for RSS feed fetches (default: 30s)
- `RSS_MAX_RETRIES` - Retry attempts for failed RSS feeds (default: 3)
- `RSS_RETRY_DELAY` - Delay between RSS retries (default: 5.0s)
- `GDELT_MAX_RETRIES` - Max retries for GDELT rate limits (default: 7)
- `GDELT_BASE_DELAY` - Base delay for GDELT exponential backoff (default: 10.0s)

### Curation UI
- `CURATION_ENABLED` - Enable/disable curation UI (default: `true`)
- `CURATION_USER` - HTTP Basic auth username
- `CURATION_PASSWORD` - HTTP Basic auth password

### Cleanup
- `PYTHONUNBUFFERED` - Force unbuffered output (set to `1` in CI)

---

## Daily Operations

### 1. Ingestion Pipeline (Runs 2x daily via GitHub Actions)
**Schedule:** 06:00 and 18:00 UTC (configurable via `CRON_SCHEDULE`)

**Manual trigger:**
```bash
# Local
export DATABASE_URL=...
export GROQ_API_KEY=...
uv run python -m src.ingestion.run

# Dry run (no DB writes)
uv run python -m src.ingestion.run --dry-run
```

**Expected output:**
```
Phase 1: Ingesting articles...
  RSS: 45, GDELT: 12, Reddit: 8
  Total fetched: 65, New articles: 52
Phase 2: Building reporting units...
  Reporting units created: 18
Phase 3: Building stories...
  Stories created/modified: 7
Phase 4: Applying tier-1 gate...
  Stories queued: 3, blocked: 4
```

**Failure modes:**
| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| `DATABASE_URL not configured` | Missing secret | Add DATABASE_URL to GitHub/Vercel env |
| `GDELT rate limited (429)` | Too many requests | Increase `GDELT_THROTTLE_SECONDS` |
| `RSS fetch timeout` | Slow feed | Increase `RSS_FETCH_TIMEOUT` |
| `0 new articles` | All dupes or no news | Check `status_log` table for details |

### 2. Weekly Cleanup (Runs Sunday 03:00 UTC)
**Schedule:** Weekly via GitHub Actions

**Manual trigger:**
```bash
uv run scripts/cleanup_stale.py --pending-hours=72 --blocked-hours=168 --queued-hours=0
```

**Behavior:**
- PENDING → EXPIRED after 72h (3 days)
- BLOCKED → EXPIRED after 168h (7 days)
- QUEUED → **NEVER** expires by default (opt-in via `--queued-hours`)

### 3. Curation UI (Vercel Deployment)
**Deploy:**
```bash
vercel --prod
# or push to main branch
```

**Access:**
- URL: `https://your-project.vercel.app`
- Auth: HTTP Basic (`CURATION_USER` / `CURATION_PASSWORD`)

**Workflow:**
1. Open dashboard → see QUEUED stories (passed tier-1 gate)
2. Press **E** to edit / **A** to approve / **R** to reject
3. In edit: select platform, review caption, save
4. Go to `/posts` → **Mark Posted** when published

---

## Metrics & Monitoring

### Health Check
```bash
curl https://your-project.vercel.app/healthz
```

**Response example:**
```json
{
  "python": "3.12.5",
  "on_vercel": true,
  "env_set": {
    "DATABASE_URL": true,
    "GROQ_API_KEY or CEREBRAS_API_KEY": true,
    "CURATION_USER and CURATION_PASSWORD": true
  },
  "database_url": {"scheme": "postgresql", "host": "<db-host>", "port": 5432, "db": "postgres"},
  "db_connect": "ok",
  "stories_by_status": {"PENDING": 5, "QUEUED": 3, "BLOCKED": 2, "REJECTED": 10},
  "approved_posts": 42,
  "verdict": "ok"
}
```

### Metrics Endpoint (Prometheus-style)
```bash
curl https://your-project.vercel.app/metrics
```

**Response example:**
```json
{
  "status_log_by_phase": {"ingest_ok": 48, "verify_ok": 48, "group_ok": 48, "gate_ok": 48},
  "stories_by_status": {"PENDING": 5, "QUEUED": 3, "BLOCKED": 2, "REJECTED": 10, "POSTED": 42},
  "articles_by_tier_24h": {"TIER1": 45, "TIER2": 20, "TIER3": 5},
  "curation_by_status": {"DRAFT": 2, "APPROVED": 5, "POSTED": 42, "REJECTED": 3},
  "gdelt_domains_24h": {"bbc.com_true": 24, "theguardian.com_true": 23, "npr.org_false": 1}
}
```

---

## Troubleshooting

### Database Connection Issues
```bash
# Check DATABASE_URL format
uv run python -c "
from src.shared.database import describe_database_url
import os
print(describe_database_url(os.getenv('DATABASE_URL')))
"
```

**Common fixes:**
- Password with special chars → URL-encode: `p%40ss%21word`
- IPv6 issues → Force IPv4 in connection string: `host=db.example.com` not `[::1]`
- Connection pool exhausted → Reduce concurrent workers

### GDELT Rate Limiting
```bash
# Check recent GDELT health
curl https://your-project.vercel.app/metrics | jq '.gdelt_domains_24h'
```

**Adjust throttle:**
```yaml
# In GitHub Actions workflow
env:
  GDELT_THROTTLE_SECONDS: "60"  # Increase from 30
  GDELT_MAX_RETRIES: "10"        # Increase from 7
```

### Stuck Stories
```sql
-- Find stories stuck in PENDING > 72h
SELECT * FROM stories 
WHERE status = 'PENDING' 
AND updated_at < NOW() - INTERVAL '72 hours';

-- Find QUEUED stories (may need curator action)
SELECT * FROM stories WHERE status = 'QUEUED';
```

### Caption Validation Failures
Edit form shows validation error → Click "Override validation" checkbox → Save

**Common issues:**
| Error | Fix |
|-------|-----|
| "exceeds 280 chars" | Shorten caption |
| "missing source URL" | Add URL from article list |
| "verbatim text detected" | Paraphrase more aggressively |

---

## Deployment Checklist

### Vercel (Curation UI)
- [ ] `DATABASE_URL` set in Vercel project env
- [ ] `GROQ_API_KEY` or `CEREBRAS_API_KEY` set
- [ ] `CURATION_USER` and `CURATION_PASSWORD` set
- [ ] `CURATION_ENABLED=true` set
- [ ] Custom domain configured (optional)
- [ ] Function timeout ≥ 60s (Vercel Functions default 300s)

### GitHub Actions (Ingestion + Cleanup)
- [ ] `DATABASE_URL` in repository secrets
- [ ] `GROQ_API_KEY` / `CEREBRAS_API_KEY` in secrets
- [ ] `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` in secrets
- [ ] `YOUTUBE_API_KEY` in secrets (optional)
- [ ] Workflow schedules enabled (not disabled by 60-day inactivity)

### Database
- [ ] Tables created (`scripts/init_db.py` or migration)
- [ ] Indexes exist on `stories.status`, `raw_articles.url_hash`
- [ ] Enum types match SQLAlchemy models

---

## Emergency Procedures

### Disable Ingestion Immediately
```yaml
# GitHub Actions workflow
env:
  GDELT_ENABLED: "false"
```

### Disable Curation UI Immediately
```bash
# In Vercel project settings
CURATION_ENABLED=false
```

### Clear Stuck Pipeline
```bash
# 1. Check status_log for latest failure
SELECT * FROM status_log ORDER BY created_at DESC LIMIT 10;

# 2. If ingestion stuck, check raw_articles table
SELECT count(*) FROM raw_articles WHERE fetched_at > NOW() - INTERVAL '24 hours';

# 3. Re-run manually with debug
DEBUG=1 uv run python -m src.ingestion.run --dry-run
```

---

## Key SQL Queries

```sql
-- Pipeline health (last 24h)
SELECT phase, status, count(*) 
FROM status_log 
WHERE created_at > NOW() - INTERVAL '24 hours' 
GROUP BY phase, status;

-- Story pipeline
SELECT status, count(*) FROM stories GROUP BY status;

-- Top entities (last 7 days)
SELECT entity_text, count(*) 
FROM story_entities 
JOIN stories ON stories.id = story_entities.story_id 
WHERE stories.created_at > NOW() - INTERVAL '7 days' 
GROUP BY entity_text 
ORDER BY count DESC LIMIT 20;

-- Curation funnel
SELECT 
  (SELECT count(*) FROM stories WHERE status = 'QUEUED') as queued,
  (SELECT count(*) FROM curated_posts WHERE status = 'APPROVED') as approved,
  (SELECT count(*) FROM curated_posts WHERE status = 'POSTED') as posted;

-- GDELT domain success rate
SELECT 
  details->>'domain' as domain,
  SUM(CASE WHEN details->>'ok' = 'true' THEN 1 ELSE 0 END) as ok,
  SUM(CASE WHEN details->>'ok' = 'false' THEN 1 ELSE 0 END) as failed
FROM status_log 
WHERE phase = 'ingest' 
AND details ? 'domain'
AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY domain;
```

---

## Contacts

- **On-call:** Check GitHub Actions workflow runs for failures
- **Database:** Supabase / Neon / PostgreSQL provider dashboard
- **LLM:** Groq / Cerebras console for API quota