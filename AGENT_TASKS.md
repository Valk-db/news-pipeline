# AGENT_TASKS.md v12

## Diagnosis: Supabase preview branch failure

`supabase/config.toml` line 65-70:

```
[db.seed]
enabled = true
sql_paths = ["./seed.sql"]
```

`supabase/seed.sql` does not exist anywhere in the repo (`ls supabase/*.sql` finds nothing —
only `config.toml` under `supabase/`). This is the default template `supabase init` writes;
nobody disabled it or added a seed file.

Supabase's preview-branch provisioning runs `supabase db reset` (or equivalent) against a
fresh database for the branch: apply all migrations, then seed. With seeding enabled and
`./seed.sql` missing, that step fails outright — this is almost certainly the "preview failed"
error, and it would fail on every PR, not just this one.

This gap exists because nothing in CI exercises the Supabase CLI at all: `.github/workflows/
ci.yml` line 81 runs `scripts/init_db.py` against a raw Postgres service container (SQLAlchemy
`create_all`, not `supabase db reset`), so a broken `supabase/config.toml` or migration set has
no automated check and only surfaces when Supabase itself tries to provision a branch.

## P0 — Fix the seed config

File: `supabase/config.toml`

There is no seed data anywhere in this repo (tables are populated by the ingestion pipeline,
not a seed script). Set:

```
[db.seed]
enabled = false
```

Leave `sql_paths` as-is or remove it — it's inert once `enabled = false`.

## P1 — If the preview still fails after P0, check this next

`supabase/migrations/20260924000000_phase1_viewpoint_schema.sql` and
`20260924000100_stories_status_expired.sql` both carry a header comment saying they must be
"run as TWO separate executions" / "by itself" in the SQL editor, because Postgres won't let
a transaction use a new enum value it just added in the same transaction. Neither file actually
uses the new enum value elsewhere in the same file, so they should be safe to run as a single
statement batch — but confirm this by running `supabase db reset` locally (or against a scratch
Supabase project) against a clean database and watching whether either file errors on
"unsafe use of new value of enum type." If it does, split each into two migration files (one
that only does `ALTER TYPE ... ADD VALUE`, a second with everything else) so the CLI's
per-file transaction boundary matches what the original comment assumed about the SQL editor.

Do not touch these files speculatively — only act on P1 if P0 alone doesn't resolve the
preview failure, since inserting a migration split when it isn't needed just adds two more
files to review for no reason.

## Verification

- `supabase db reset` (or `supabase start` + `supabase db reset` locally with the CLI, against
  Docker or a scratch project) completes without error after P0.
- Push and confirm the next PR's Supabase preview branch provisions successfully.
- No application code changes in this task — do not touch `src/` or `curation_ui/`.