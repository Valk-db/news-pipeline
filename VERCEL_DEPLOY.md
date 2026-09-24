# Deploying Curation UI to Vercel

This guide walks through deploying the FastAPI curation UI to Vercel with a Supabase PostgreSQL database.

## Prerequisites

1. **Vercel account** - Sign up at [vercel.com](https://vercel.com)
2. **Supabase project** - Create at [supabase.com](https://supabase.com) (free tier works)
3. **Vercel CLI** - Install globally: `npm i -g vercel`

## 1. Set up Supabase Database

1. Create a new Supabase project
2. Go to **SQL Editor** and run:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
3. Go to **Settings → Database** and copy the **Connection string** (URI format)
   - **Direct (IPv6, may fail in CI/GitHub Actions):** `postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres`
   - **Pooler (IPv4, RECOMMENDED for CI/edge):** `postgresql+asyncpg://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres`
   - Use the **Pooler** string for GitHub Actions and Vercel deployments
4. Run the database initialization locally to create tables:
   ```bash
   cd news-pipeline
   cp .env.example .env
   # Edit .env with your Supabase connection string
   uv run scripts/init_db.py
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

vercel env add GROQ_API_KEY
# Paste your Groq API key

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
| `GROQ_API_KEY` | Yes | `gsk_xxx` |
| `GROQ_MODEL` | No | `openai/gpt-oss-20b` |
| `CEREBRAS_API_KEY` | No | `csk_xxx` |
| `CEREBRAS_MODEL` | No | `gpt-oss-120b` |
| `REDDIT_USER_AGENT` | No | `news-pipeline/0.1` |
| `YOUTUBE_API_KEY` | No | `xxx` |

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
2. You should see the curation UI with pending stories
3. Test keyboard shortcuts: **A**pprove, **R**eject, **E**dit

## Troubleshooting

### Database Connection Issues

If you see database errors:
- Ensure `DATABASE_URL` uses `postgresql+asyncpg://` (not `postgres://`)
- Check that Supabase allows connections from Vercel IPs (default: allows all)
- Verify `pgvector` extension is enabled in Supabase

### Function Timeout

The function is configured for 300s max duration. If you hit timeouts:
- Check database query performance
- Ensure indexes exist on `stories.status`, `stories.day`, `stories.created_at`

### Static Files Not Loading

The FastAPI app self-mounts static files via `app.mount("/static", StaticFiles(...))` in `curation_ui/main.py:40`. No Vercel rewrites are needed. If CSS doesn't load:
- Check browser dev tools for 404s on `/static/style.css`
- Verify the `static/` folder is included in deployment (not in `.vercelignore`)
- Ensure the `curation_ui/` directory is deployed (not in `.vercelignore`)

### Import Errors

If you see module import errors:
- Ensure all dependencies are in `pyproject.toml` (project uses uv, not requirements.txt)
- Check that `src/` folder is NOT in `.vercelignore` (it contains shared code)

## Configuration Files

### `vercel.json`
```json
{
  "functions": {
    "curation_ui/main.py": {
      "maxDuration": 300
    }
  }
}
```

**Routing verified against the live deployment** (`https://<your-app>.vercel.app`, 2026-09-23): the FastAPI app is served at root with no path prefix or rewrite. `/healthz` → 200 (JSON body, `on_vercel: true`); `/`, `/posts`, `/story/{story_id}/edit`, `/post/{post_id}/mark-posted` → 401 (auth required, FastAPI JSON `{"detail":"Unauthorized"}`); a nonexistent path → 404. All documented non-root paths resolve correctly.

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