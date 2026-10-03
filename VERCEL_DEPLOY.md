# Deploying Curation UI to Vercel

This guide walks through deploying the FastAPI curation UI to Vercel with a Supabase PostgreSQL database.

## Prerequisites

1. **Vercel account** - Sign up at [vercel.com](https://vercel.com)
2. **Supabase project** - Create at [supabase.com](https://supabase.com) (free tier works)
3. **Vercel CLI** - Install globally: `npm i -g vercel`

## 1. Set up Supabase Database

1. Create a new Supabase project
2. Apply the schema — `supabase/migrations/*.sql` is the only thing that creates it, so either run
   `uv run python -m scripts.migrate` locally against the new database or paste the files into the
   **SQL Editor** in order. No extension needs enabling by hand: `pg_trgm` is created by its own
   migration, and so is `pgvector` (`20261002220200_pgvector_readiness.sql`). No column uses pgvector
   yet — embeddings are still JSON array columns
3. Go to **Settings → Database** and copy the **Connection string** (URI format)
   - **Direct (IPv6, may fail in CI/GitHub Actions):** `postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres`
   - **Pooler (IPv4, RECOMMENDED for CI/edge):** `postgresql+asyncpg://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres`
   - Use the **Pooler** string for GitHub Actions and Vercel deployments
4. Check the result:
   ```bash
   cd news-pipeline
   cp .env.example .env
   # Edit .env with your Supabase connection string
   uv run python scripts/migrate.py   # idempotent; safe to re-run
   uv run python scripts/check_schema.py
   ```

## 2. Configure Environment Variables in Vercel

### Option A: Using Vercel CLI (Recommended)

```bash
# Login to Vercel
vercel login

# Link your project (run from project root)
vercel link

# Add environment variables
vercel env add DATABASE_URL
# Paste your Supabase connection string when prompted

vercel env add CURATION_USER
vercel env add CURATION_PASSWORD
# Without both, every authenticated route answers 503 (see the table below)

vercel env add GROQ_API_KEY
# Paste your Groq API key (optional: no UI surface needs it — the curation app is read-only)

vercel env add GROQ_MODEL
# Enter: openai/gpt-oss-20b

vercel env add CEREBRAS_API_KEY
# Paste your Cerebras API key (optional)

vercel env add REDDIT_USER_AGENT
# Enter: news-pipeline/0.1
```

### Option B: Using Vercel Dashboard

1. Go to your project in Vercel Dashboard
2. Navigate to **Settings → Environment Variables**
3. Add each variable from the table below:

| Variable | Required | Example |
|----------|----------|---------|
| `DATABASE_URL` | Yes | `postgresql+asyncpg://postgres:xxx@db.xxx.supabase.co:5432/postgres` |
| `CURATION_USER` | Yes | `curator` |
| `CURATION_PASSWORD` | Yes | (any strong secret) |
| `GROQ_API_KEY` | No | `gsk_xxx` |
| `GROQ_MODEL` | No | `openai/gpt-oss-20b` |
| `CEREBRAS_API_KEY` | No | `csk_xxx` |
| `CEREBRAS_MODEL` | No | `gpt-oss-120b` |
| `REDDIT_USER_AGENT` | No | `news-pipeline/0.1` |
| `YOUTUBE_API_KEY` | No | `xxx` |

`CURATION_USER` + `CURATION_PASSWORD` are what `require_auth` checks
(`curation_ui/security.py`); with either one missing it raises 503
"Curation UI not configured" instead of authenticating, so the UI is unusable
without them. The LLM keys are optional for the deployment and unused by it:
`/healthz/details` reports whether one is set, and nothing else in the curation
app calls an LLM. `/healthz` works with no database at all (it reports `degraded`).

## 3. Deploy to Vercel

```bash
# From project root
vercel --prod
```

Or for preview deployment:
```bash
vercel
```

## 4. Verify Deployment

1. Open the deployment URL provided by Vercel
2. `GET /healthz` → 200 with `{"status", "database"}` — `ok`/`ok` only when the database answers,
   `degraded` otherwise (anonymous, and it never returns a secret)
3. The UI itself asks for HTTP Basic credentials — a browser that gets 401 there means auth is
   wired, not broken. `GET /healthz/details` with the same credentials is the diagnostic page
4. Open `/map` (public) and `/` (Basic auth) — both are read-only; there is no triage control on
   either page, and there is no keyboard shortcut any more (approve/reject/edit were removed
   on 2026-10-02)

## Troubleshooting

### Database Connection Issues

If you see database errors:
- Ensure `DATABASE_URL` uses `postgresql+asyncpg://` (not `postgres://`)
- Check that Supabase allows connections from Vercel IPs (default: allows all)
- `GET /healthz/details` names the failing step; it scrubs the DB host out of error text

### Function Timeout

The function is configured for 60s max duration (`vercel.json`). If you hit timeouts:
- Check database query performance
- Ensure indexes exist on `stories.status` and `stories.day` (created by the core migration) and
  on `stories.created_at` if you added it

### Static Files Not Loading

The FastAPI app self-mounts static files via `app.mount("/static", StaticFiles(...))` in
`curation_ui/main.py:64`, and `vercel.json` rewrites every path to the single function, so the
app — not Vercel — serves `/static`. If CSS doesn't load:
- Check browser dev tools for 404s on `/static/style.css`
- Verify `curation_ui/static/` is included in deployment (not in `.vercelignore`)

### Import Errors

If you see module import errors:
- Vercel's Python runtime installs from `pyproject.toml` `[project.dependencies]` (the curated UI
  list) and from `requirements.txt` if present — this repo has both, and they are not in sync
  (`requirements.txt` still pins `slowapi` and `scikit-learn`, neither of which the UI imports).
  Anything the function imports has to be in the pyproject list.
- `.vercelignore` drops `src/ingestion/`, `src/utils/`, `src/verification/` and re-includes
  `src/shared/` and `src/schema/`, which is exactly what `curation_ui/` imports. Keep it that way.

## Configuration Files

### `vercel.json`
```json
{
  "functions": {
    "api/index.py": { "maxDuration": 60 }
  },
  "rewrites": [
    { "source": "/(.*)", "destination": "/api/index" }
  ],
  "crons": [
    { "path": "/api/cron/checkpoint", "schedule": "0 6 * * *" },
    { "path": "/api/cron/checkpoint/watchdog", "schedule": "30 6 * * *" }
  ]
}
```

The two cron paths are served by `curation_ui/cron.py` and are bearer-token routes, not
`require_auth` — Vercel Cron sends a GET with an `Authorization` header and no page to have
issued a token. They refuse outright (503, not 401) when `TRANSPARENCY_CRON_TOKEN` is unset.

`api/index.py` is the function Vercel builds; it is three lines that re-export the ASGI app
(`from curation_ui.main import app`). The catch-all rewrite sends every request to it, so the
FastAPI app sees the original path. `pyproject.toml` also sets
`[tool.vercel] entrypoint = "curation_ui.main:app"`, which points at the same app.

**Routing** (verified against the code, not a live deployment): `/healthz` → 200 with
`{"status", "database"}` only; `/healthz/details` → the topology report behind
`require_auth`; `/`, `/story/{id}`, `/api/stories/{id}/viewpoints` and
`/api/stories/{id}/sources` → 401 without credentials; `/map`, `/stories/{id}`,
`/proof/{id}` and the `/api/map/*` and `/api/globe/*` JSON → anonymous; a nonexistent
path → 404. There are no state-changing routes and nothing requires `X-CSRF-Token`
any more: approve, reject, edit, save and the four CSRF-guarded POSTs were removed
on 2026-10-02, and `tests/test_route_table.py` pins both facts.

## Cost

- **Vercel Hobby**: Free for personal projects
- **Supabase Free**: 500 MB database, 1 GB bandwidth
- **Groq**: Free tier (1K req/day, 200K tokens/day)

Total: **$0/month** for typical usage.

## Local Development with Vercel Env

Pull environment variables for local testing:
```bash
vercel env pull .env.local
```

Then run locally:
```bash
uv run curation_ui/main.py
# Opens http://localhost:8000
```