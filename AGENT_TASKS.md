# AGENT_TASKS.md v13

Supersedes v12 (seed config + enum migration split — both already merged in `03d27b09`).
Three new/still-open bugs, each root-caused against the actual repo history and code below.

---

## P0-A — "Remote migration versions not found in local migrations directory"

### Diagnosis

Commit `92aec20bd8` ("chore: P3 - cleanup pass") did this rename:

```
rename from supabase/migrations/20260925000000_globe_events_schema.sql
rename to   supabase/migrations/20260924000200_globe_events_schema.sql
```

That changes the migration's *version* (the timestamp prefix) from `20260925000000` to
`20260924000200` — it wasn't just a content edit. If that file had already been applied
anywhere under its old name (production, or this PR's own preview branch from an earlier
push — preview branches persist across pushes to the same PR and apply new pending
migrations each time), that database's `supabase_migrations.schema_migrations` table now
has a row for version `20260925000000` that no local file matches, while a "new" version
`20260924000200` exists locally that database has never recorded as applied. That mismatch
is exactly what produces "Remote migration versions not found in local migrations
directory."

Confirmed by walking the migrations folder's commit history — this is the *only* rename
that ever touched a migration file's timestamp; every other change (`035b875a75`,
`832c5dcd59`, `142f11145e`, `03d27b09`) only added new files or edited a file's content
without changing its version prefix.

### Fix

Renaming an already-applied migration is the anti-pattern here — don't do it again. For
this occurrence:

1. **If the affected environment is only this PR's preview branch** (most likely): delete
   and let the preview branch reprovision from scratch. It's ephemeral and disposable —
   this sidesteps needing any history surgery. Push a trivial commit or close/reopen the PR
   to force reprovisioning, then confirm the Supabase preview check goes green.
2. **If a persistent branch/production was affected** (only if step 1 doesn't clear the
   error, or the error also shows up outside preview branches): reconcile history instead
   of re-running SQL, using the CLI against that specific project/branch:
   ```
   supabase migration repair --status reverted 20260925000000
   supabase migration repair --status applied 20260924000200
   ```
   This tells Supabase "the old version number is gone, the new one is already applied" —
   it does not re-run any SQL (the underlying statements are `CREATE TABLE IF NOT EXISTS` /
   idempotent already, so re-running would be harmless anyway, but repair is the correct
   tool for a pure history mismatch).

Going forward: treat a migration's filename/timestamp as immutable once it's merged to
`main` — if a date needs correcting, do it before merge, or land a *new* migration instead
of renaming an old one.

---

## P0-B — Internal Server Error on the main curation page (`GET /`)

### Diagnosis

The latest commit (`03d27b09`) extended `_render_stories_grid()` in `curation_ui/main.py`
(called unconditionally by `GET /`) to query `MediaAsset`, `Snippet`, and
`SourceReliabilitySnapshot` — three tables that back the Phase 2 (multimedia/snippets) and
Phase 3 (reliability) features.

**None of these tables exist in `supabase/migrations/`.** Checked every file in that
directory — only `sourcetier`/`stories` column changes, the `status` enum, and
`event_*`/`canonical_entities` tables are covered. `media_assets`, `snippets`,
`source_reliability_snapshots`, `fact_check_records`, `correction_records`,
`article_embeddings`, and `story_embeddings` exist only as SQLAlchemy models
(`src/schema/models.py`) — they were added across the Phase 2/3 commits with no
corresponding migration.

The only thing that ever creates them is `init_db()` (`Base.metadata.create_all`,
`src/shared/database.py`), and that runs in exactly two places:
- `ci.yml`'s test job, against a throwaway container — so CI never notices the tables are
  missing from real migrations, because CI builds its schema straight from the models.
- `weekly-enrichment.yml`'s `enrichment` job, against the real `DATABASE_URL`, but only on
  its Sunday 02:00 UTC cron.

It is **never called** by `curation_ui/main.py` (no `init_db()` import there) — the
Vercel-deployed curation UI has no path that creates these tables. If Phase 2/3 landed less
than a week before someone opened the curation page, `weekly-enrichment` hasn't run yet
either, so the tables genuinely don't exist yet in that database. `_render_stories_grid()`
hits `media_assets`/`snippets`/`source_reliability_snapshots` unconditionally on every load
of `/`, throws `UndefinedTableError`, and FastAPI turns that into a 500.

This also explains why nothing caught it before merge: `src/shared/schema_check.py`
explicitly treats missing tables as *not* drift —
```python
def has_drift(report: dict[str, list[Any]]) -> bool:
    """
    True iff missing_columns or missing_enum_labels is non-empty.
    Missing tables alone are NOT drift, because init_db() creates them.
    """
```
— and `scripts/check_schema.py` (run in `daily-ingest.yml` against production) prints
`INFO table will be created by init_db: media_assets` and exits 0. The comment's assumption
("init_db() creates them") is only true for the two execution paths above, not for the
curation UI, so this check gave a false all-clear for exactly the bug that shipped.

### Fix

**1. Add the missing migration.** New file
`supabase/migrations/20260924000300_phase2_phase3_missing_tables.sql`:

```sql
-- Phase 2 (Multimedia & Snippet Enrichment) and Phase 3 (Historical Reliability Rating)
-- schema. These tables existed only as SQLAlchemy models until now; nothing under
-- supabase/migrations/ ever created them, so any environment not bootstrapped by
-- init_db() (Base.metadata.create_all) -- including the deployed curation UI -- was
-- missing them entirely. Idempotent: safe to re-run.

-- Enum labels match the Python enum members' NAMES (uppercase), not their lowercase
-- .value strings: none of these Enum() columns declare values_callable, so SQLAlchemy
-- binds/queries using the member name by default -- same convention as the existing
-- sourcetier ('TIER4') and status ('EXPIRED') enums.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'mediatype') THEN
        CREATE TYPE mediatype AS ENUM ('IMAGE', 'VIDEO', 'AUDIO', 'EMBED');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'snippettype') THEN
        CREATE TYPE snippettype AS ENUM ('QUOTE', 'STAT', 'FACT', 'SUMMARY', 'CLAIM');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'verdict') THEN
        CREATE TYPE verdict AS ENUM ('TRUE', 'MOSTLY_TRUE', 'MIXED', 'MOSTLY_FALSE', 'FALSE', 'UNVERIFIED');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'factchecker') THEN
        CREATE TYPE factchecker AS ENUM ('CLAIMBUSTER', 'LLM_VERIFIER', 'CLAIMREVIEW', 'MANUAL');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'severity') THEN
        CREATE TYPE severity AS ENUM ('MINOR', 'MODERATE', 'MAJOR', 'RETRACTION');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS media_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID REFERENCES raw_articles(id) ON DELETE CASCADE,
    story_id UUID REFERENCES stories(id) ON DELETE CASCADE,
    media_type mediatype NOT NULL,
    url TEXT NOT NULL,
    thumbnail_url TEXT,
    alt_text TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds INTEGER,
    source VARCHAR(100),
    source_id VARCHAR(100),
    meta_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_media_assets_article_id ON media_assets(article_id);
CREATE INDEX IF NOT EXISTS ix_media_assets_story_id ON media_assets(story_id);
CREATE INDEX IF NOT EXISTS ix_media_assets_type ON media_assets(media_type);

CREATE TABLE IF NOT EXISTS snippets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    snippet_type snippettype NOT NULL DEFAULT 'QUOTE',
    text TEXT NOT NULL,
    position INTEGER,
    entities JSONB,
    minhash_signature JSONB,
    confidence INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_snippets_story_id ON snippets(story_id);
CREATE INDEX IF NOT EXISTS ix_snippets_article_id ON snippets(article_id);
CREATE INDEX IF NOT EXISTS ix_snippets_type ON snippets(snippet_type);

CREATE TABLE IF NOT EXISTS source_reliability_snapshots (
    id SERIAL PRIMARY KEY,
    source_domain VARCHAR(255) NOT NULL,
    snapshot_date TIMESTAMPTZ NOT NULL,
    factual_accuracy INTEGER,
    correction_rate INTEGER,
    consensus_alignment INTEGER,
    transparency_score INTEGER,
    reliability_score INTEGER NOT NULL,
    total_claims_verified INTEGER NOT NULL DEFAULT 0,
    claims_true INTEGER NOT NULL DEFAULT 0,
    claims_false INTEGER NOT NULL DEFAULT 0,
    claims_mixed INTEGER NOT NULL DEFAULT 0,
    corrections_count INTEGER NOT NULL DEFAULT 0,
    articles_sampled INTEGER NOT NULL DEFAULT 0,
    tier_at_snapshot VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_source_date UNIQUE (source_domain, snapshot_date)
);
CREATE INDEX IF NOT EXISTS ix_source_reliability_source_date ON source_reliability_snapshots(source_domain, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_source_reliability_date ON source_reliability_snapshots(snapshot_date);

CREATE TABLE IF NOT EXISTS fact_check_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    article_id UUID REFERENCES raw_articles(id) ON DELETE SET NULL,
    claim TEXT NOT NULL,
    claim_hash VARCHAR(64) NOT NULL,
    verdict verdict NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 50,
    fact_checker factchecker NOT NULL,
    fact_checker_url TEXT,
    explanation TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_date TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_fact_check_source_date ON fact_check_records(source_domain, checked_at);
CREATE INDEX IF NOT EXISTS ix_fact_check_verdict ON fact_check_records(verdict);
CREATE INDEX IF NOT EXISTS ix_fact_check_claim_hash ON fact_check_records(claim_hash);

CREATE TABLE IF NOT EXISTS correction_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    article_id UUID REFERENCES raw_articles(id) ON DELETE SET NULL,
    original_text TEXT NOT NULL,
    corrected_text TEXT NOT NULL,
    correction_summary TEXT,
    severity severity NOT NULL DEFAULT 'MODERATE',
    correction_date TIMESTAMPTZ NOT NULL,
    correction_url TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_correction_source_date ON correction_records(source_domain, correction_date);
CREATE INDEX IF NOT EXISTS ix_correction_article ON correction_records(article_id);
CREATE INDEX IF NOT EXISTS ix_correction_severity ON correction_records(severity);

CREATE TABLE IF NOT EXISTS article_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    model VARCHAR(100) NOT NULL,
    embedding JSONB NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_article_embeddings_article_id ON article_embeddings(article_id);
CREATE INDEX IF NOT EXISTS ix_article_embeddings_model ON article_embeddings(model);

CREATE TABLE IF NOT EXISTS story_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    model VARCHAR(100) NOT NULL,
    embedding JSONB NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_story_embeddings_story_id ON story_embeddings(story_id);
CREATE INDEX IF NOT EXISTS ix_story_embeddings_model ON story_embeddings(model);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id SERIAL PRIMARY KEY,
    canonical_entity_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    alias VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_canonical_alias UNIQUE (canonical_entity_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_entity_aliases_alias ON entity_aliases(alias);

-- Close the anonymous REST API path on every new table, matching scripts/enable_rls.py's
-- policy for all other tables (service-role backend bypasses RLS; no policies = no anon access).
ALTER TABLE media_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE snippets ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_reliability_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_check_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE correction_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE article_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE story_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_aliases ENABLE ROW LEVEL SECURITY;
```

Scope note: this covers *every* table that's in the same boat (models added, no
migration), not just the three hit by `/` today — `fact_check_records` is already queried
by `src/reliability/fact_checker.py` from `weekly-enrichment.yml`'s `reliability` job, so
it's the same latent bug just not reported yet. Fixing only the three that paged someone
would leave the rest to fail on their own schedule.

I've included `ENABLE ROW LEVEL SECURITY` directly in the migration so these tables aren't
briefly open to the anon key before someone remembers to run `scripts/enable_rls.py` by
hand (the documented process today).

**2. Stop letting "missing tables" pass as a clean schema check.** In
`src/shared/schema_check.py`, `has_drift()`'s premise is false outside of CI/weekly-cron —
the curation UI never runs `init_db()`. Update it (and the docstring) to also flag missing
tables as drift:
```python
def has_drift(report: dict[str, list[Any]]) -> bool:
    """True iff any of missing_tables, missing_columns, or missing_enum_labels is non-empty."""
    return bool(report["missing_tables"]) or bool(report["missing_columns"]) or bool(report["missing_enum_labels"])
```
`scripts/check_schema.py`'s existing `"INFO table will be created by init_db"` print can
stay for visibility, but the workflow should now fail (`daily-ingest.yml`'s schema-drift
step) when tables are missing, instead of silently exiting 0.

**3. Fix the README's guidance**, which currently tells contributors new tables don't need
a migration:
> `init_db()` only creates missing tables; any change to a column or enum on an **EXISTING**
> table needs an idempotent SQL file in `supabase/migrations/`...

That's the policy that produced this bug. Reword to: every new table also needs a migration
— `init_db()` is a CI/dev convenience, not a deployment mechanism, since it never runs
against the tables the deployed curation UI or the daily-ingest job depend on before the
weekly-enrichment cron happens to hit them first.

---

## P1 — Globe page "y.isDestroyed" error, still happening after the earlier fix

### Diagnosis

Two separate, concrete bugs remain in `curation_ui/static/globe/`; the earlier fix
(`2477253376`) only patched 4 call sites and missed both of these.

**1. The terrain provider is broken.** `globe-core.js`:
```js
terrainProvider: new Cesium.CesiumTerrainProvider({
    url: 'https://assets.cesium.com/terrain',
    requestWaterMask: true,
    requestVertexNormals: true
}),
```
Cesium's own docs for the pinned version (1.115, per the skybox URLs in the same file) are
explicit: *"To construct a CesiumTerrainProvider, call `CesiumTerrainProvider.fromIonAssetId`
or `CesiumTerrainProvider.fromUrl`. Do not call the constructor directly."* Passing `url` to
`new CesiumTerrainProvider(...)` is no longer supported (this changed around 1.101–1.104,
well before 1.115) — it does not synchronously become a working provider the way the
deprecated `createWorldTerrain()` helper used to. On top of that, `Cesium.Ion.defaultAccessToken`
is set to `''` two lines earlier (done deliberately when imagery was switched to OSM to
avoid needing an Ion token) — but `assets.cesium.com/terrain` is a token-gated Cesium Ion
endpoint, so even a correctly-constructed provider would get rejected. Every terrain tile
request the scene makes therefore fails, continuously, as the camera moves — and repeated
terrain-provider failures during the render loop are what surface as an internal Cesium
object's `isDestroyed()` check blowing up on a torn-down tile/terrain-data object. This
matches "still get the error" well: the earlier fix guarded *callers* of `viewer`, not the
thing that's actually erroring inside Cesium's own render loop.

The globe only displays point/polygon news events, it has no need for real elevation data —
the OSM-imagery fix's whole point was "no token needed," and the terrain provider should
match that, not silently reintroduce a token dependency.

**2. `flyToEvent`, `hideEventDetail`, and `openStory` are each defined twice** — once in
`globe-core.js`, once in `globe-interaction.js` — as bare top-level `function` declarations.
These files are loaded as plain `<script>` tags (confirmed: ES modules were deliberately
removed in `b39009322c`), so every top-level function becomes a `window` property, and
`globe.html` loads them in this order:
```
globe-core.js → globe-layers.js → globe-timeline.js → globe-interaction.js → globe-init.js
```
`globe-interaction.js` loads after `globe-core.js`, so its copies silently win; the
`globe-core.js` versions are dead code. The winning `flyToEvent` (in `globe-interaction.js`,
wired to every "Fly To" button, search result, and sidebar item's `onclick`) only checks
`if (viewer)` — not `!viewer.isDestroyed()`. Same gap in `clusterEvents()` and
`showClusterMarker()` in the same file, and in `onLeftClick`/`onMouseMove` (fires on *every*
mouse move over the globe — the highest-frequency call site in the whole module) and
`fitEvents()` in `globe-core.js`, none of which were touched by the earlier fix.

### Fix

**1. Replace the terrain provider** in `globe-core.js`:
```js
terrainProvider: new Cesium.EllipsoidTerrainProvider(),
```
removing the `CesiumTerrainProvider` block entirely (and its now-irrelevant
`requestWaterMask`/`requestVertexNormals` options, which are quantized-mesh-specific).
`Cesium.Ion.defaultAccessToken = ''` stays as-is — now genuinely nothing needs a token.

**2. Delete the shadowed duplicates** in `globe-core.js`: the `hideEventDetail()`,
`openStory()`, and `flyToEvent()` function declarations (currently around lines 467–486).
They're unreachable dead code today; deleting them just removes the confusion of two
diverging implementations.

**3. Add `viewer && !viewer.isDestroyed()` guards** to every remaining direct `viewer`/
`scene`/`camera` access that isn't already guarded:
- `globe-core.js`: `onLeftClick`, `onMouseMove`, `fitEvents()`
- `globe-interaction.js`: `flyToEvent()` (change `if (viewer)` to
  `if (viewer && !viewer.isDestroyed())`), `clusterEvents()`, `showClusterMarker()`

Pattern to use (matches the style already established in `globe-init.js`):
```js
function onMouseMove(movement) {
    if (!viewer || viewer.isDestroyed()) return;
    const picked = viewer.scene.pick(movement.endPosition);
    ...
}
```

---

## Verification

- **P0-A**: Push to the PR (or close/reopen it) and confirm the Supabase preview check
  provisions cleanly with no "Remote migration versions not found" error.
- **P0-B**: `supabase db reset` locally (or against a scratch project) applies the new
  migration with no errors; `uv run python scripts/check_schema.py` against that database
  reports no missing tables; load `/` on the curation UI and confirm it renders instead of
  500ing. `uv run pytest tests/ -v` still passes (these tables already exist in CI's
  `init_db()`-built schema, so no test changes needed).
  `uv run python scripts/enable_rls.py` can still be run as a no-op sanity check (RLS is
  now enabled directly by the migration).
- **P1**: Load `/globe`, open the browser console, move the mouse across the globe, click
  an event, use "Fit Events," fly to a search result — confirm no `isDestroyed` /
  `TypeError` in the console and that terrain tiles stop 401ing in the Network tab.

No other application code should need to change for any of these three.