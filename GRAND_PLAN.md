# GRAND_PLAN.md

The long-term vision for news-pipeline: from a defamation-safe RSS aggregator into
a certified, queryable news intelligence graph — media, sources, geography, and
narrative, ingested constantly, with receipts.

This supersedes nothing — `AGENT_TASKS.md` stays the format for "verified fix,
ready to apply" handoffs to the IDE agent. This doc is the map those tasks get
pulled from.

---

## 0. Where this stands today

Current pipeline (see `README.md` for the full picture):

```
GitHub Actions (cron) → Ingestion → Verification → Grouping → Gate → Curation UI
                              ↓
                        Supabase/Neon (Postgres + pgvector)
```

- **Ingestion**: 8 tier-1 + 11 tier-2 RSS, GDELT (disabled), Reddit tier-3
  (`src/ingestion/source_registry.py`)
- **Verification**: MinHash near-dup clustering → `ReportingUnit`
  (`src/verification/units.py`, `OWNERSHIP_GROUPS`)
- **Grouping**: entity-Jaccard → `Story`, 48h window (`src/verification/stories.py`)
- **Gate**: `Story` promoted only with ≥2 tier-1 reporting units from ≥2 distinct
  owner groups — the defamation-safe threshold
- **Already there and underused**: `cluster_viewpoints()` in `stories.py` already
  does LLM stance-labeling and sub-clusters stories by viewpoint. `CorrectionRecord`,
  `SourceReliabilitySnapshot`, `EventGeometry`/`EventLayer`/`Event` all exist in
  `src/schema/models.py`. Phase 2 and 3 below extend these, not replace them.

**Priority order for everything below (your call):**
1. Broader ingestion (more sources/sensors)
2. Narrative/story intelligence (claim matrix, arcs)
3. Verification & certification layer (claims, provenance, evidence)
4. Geography/globe expansion

**Budget stance:** strict $0/month for now. Every addition below has to survive on
a free tier. The data model and infra patterns are chosen so that switching a
free source for a paid one later, or opening the data up as a paid API, doesn't
require rework — see §6.

---

## 1. Ingestion — broader sources and sensors (Phase 1)

### 1a. New source categories

| Category | Examples | Notes |
|---|---|---|
| Primary gov/legal docs | congress.gov, SEC EDGAR, CourtListener/RECAP, WHO/UN press releases | citable directly instead of paraphrasing a paraphrase |
| Sensor/telemetry feeds | USGS earthquakes, GDACS/NIFC-style disaster feeds, ADS-B flight data, AIS ship data | ground-truth geo-events independent of any journalist — feed `Event`/`EventGeometry` directly, bypass the article pipeline entirely |
| Non-English tier-1 equivalents | BBC Arabic/Mundo, DW (multi-language), Le Monde | + a Groq translation pass — closes the "all viewpoints" gap, English-only right now |
| OSINT / social tier (tier-4/5) | Bluesky firehose, Telegram public channels | never gates a story alone under the current gate — landed as low-tier `RawArticle`, useful as leads + first-sighting timestamps |
| Leaked/whistleblower datasets | DDoSecrets, ICIJ leak databases | not feeds, periodic dataset drops — manual-curation source, lower priority |
| Podcast/broadcast transcripts | Whisper STT on a handful of shows | scoops break in audio first sometimes — later, once STT cost is worth it |

### 1b. Infra patterns worth stealing from `gods-eye-view`

Pulled from a live, deployed $0-to-mostly-$0 aggregation project
(`bilawalsidhu/gods-eye-view` — aircraft/ships/quakes/cameras on a 3D globe).
Their `DATA_SOURCES.md` and `docs/INFRASTRUCTURE-LAYERS.md` are the references.

- **Uniform source lifecycle contract.** Every one of their data layers implements
  the same interface: `init / enable / disable / update / destroy / getStats`.
  Nothing fetches on import — only on enable. Apply this to `src/ingestion/`: give
  every source module (RSS, GDELT, sensor feed, OSINT) the same shape —
  `fetch() / normalize() / health_check() / stats()` — instead of bespoke
  functions per source. Makes "add a new source" and "build a health dashboard"
  both mechanical instead of one-off.
- **Fallback chains with explicit staleness, never silent failure.** Their
  pattern everywhere: primary → secondary → last-good-cache, with the active
  tier surfaced, not hidden. Their flights layer: OpenSky → adsb.lol (capped,
  regional) → keep prior snapshot. Apply this to your gate: a story built partly
  on a degraded/stale source should carry that flag through to curation, not
  just silently use stale data.
- **Keyless-first, BYOK-optional, kill-switched per source.** Every source works
  with zero keys by default; paid/keyed sources are optional and gated behind an
  env var (their `CCTV_WARENDORF_ENABLED=0` pattern). Matches your budget stance
  exactly, and gives you a lever for later: "premium tier enables these extra
  feeds" is just flipping flags, not a rearchitecture.
- **Request budget governors + coalescing, not just rate limiting.** Their
  TomTom integration computes a daily tile budget sized against the provider's
  published monthly free cap, and de-dupes identical in-flight requests instead
  of firing twice. Directly applicable to Groq's 1K req/day: build a governor
  that tracks daily spend against the cap and coalesces duplicate LLM calls
  (e.g. two units triggering the same extraction prompt), with a serve-stale
  fallback when the budget's exhausted for the day.
- **A `DATA_SOURCES.md` ledger.** Their format: source, what it's used for,
  license/terms, attribution string, cache/rate policy — one row per source.
  Worth copying into this repo wholesale as sources get added (§1c).

### 1c. The one that matters most for the "sell access later" plan

Several free sources carry **non-commercial-only** terms, and this bites you
specifically because of the stated goal to eventually sell access:

- **OpenSky Network** (candidate for the flight-sensor feed): non-commercial
  license, and their own docs say *even non-profit operational use can require
  a prior written agreement*.
- **Google News RSS**: personal/noncommercial use only per Google's terms.
- **GDELT**, by contrast, explicitly *permits commercial dataset use with
  citation* — already in your registry (disabled), worth re-enabling with that
  in mind.

Action: create `DATA_SOURCES.md` in this repo now, before adding new source
categories, with a **license/commercial-risk column** for every source —
existing tier-1/2/3 included, retroactively. Cheap to do now; expensive to
discover after a paying customer is depending on a source you have to rip out.

---

## 2. Narrative & story intelligence (Phase 2)

Builds on `cluster_viewpoints()` in `stories.py`, which already does LLM
stance-labeling. This phase is "how do we store and navigate the relationship
data between everything" — the answer is Postgres edge tables, not a separate
graph database. You already have Supabase/pgvector; no new infra needed.

**New tables (`supabase/migrations/`, idempotent per the existing convention):**

- **`claims`** — atomic factual assertions extracted per `ReportingUnit` (LLM
  pass), linked to `story_id`, tagged by type (fact / allegation / prediction /
  quote), `first_seen_at`.
- **`claim_evidence`** — edge table: `claim_id → unit_id`, stance
  (supports/disputes/neutral), confidence. This is the "who says what" matrix
  per story.
- **`entity_edges`** — generic typed edges:
  `(subject, predicate, object, confidence, source_unit_id)`. Predicates:
  `CORROBORATES`, `DISPUTES`, `SAME_EVENT_AS`, `PART_OF_NARRATIVE`, `CAUSED_BY`.
  This is what lets a story from March link forward to one in September as the
  same ongoing narrative arc — story grouping today is a 48h window; this is
  the layer above it.
- **`topic_groups`** — hierarchical (`parent_group_id`), e.g.
  Geopolitics → Middle East → Israel-Gaza. Stories map into one or more groups.
  Feeds directly into §3's per-topic reliability scoring.

**Deliverable:** extend `cluster_viewpoints()` into a full per-story claim
matrix ("source A says X, source B says Y") rather than just a viewpoint label.

---

## 3. Verification & certification layer (Phase 3)

Turns "aggregator" into something with receipts. Extends
`SourceReliabilitySnapshot`, `FactCheckRecord`, `CorrectionRecord` — all
already in the schema.

- **`source_topic_reliability`** — `(source_id, topic_group_id, score,
  sample_size)` instead of one global reliability number. A source can be solid
  on economics and unreliable on one specific conflict.
- **The dynamic gate.** Generalizes the current static "≥2 tier-1 owners" rule
  into a scoring function the current rule becomes a special case of:

  `admission_score = f(source_tier_baseline, corroboration_count,
  distinct_owner_count, virality_signal, harm_level)`

  - `virality_signal`: reuse MinHash cluster size within tier-3/4/5 in a
    rolling window — you already compute this.
  - `harm_level`: fires when a claim names a real individual + a serious
    allegation (crime/violence/health/financial). Higher harm = higher
    corroboration bar required, regardless of virality — the defamation
    guardrail scales with the new source tiers instead of weakening as tier-4/5
    ingestion grows.
  - Net effect: a lone unverified post never gates a story alone; the same
    claim independently corroborated by several unrelated sources climbs the
    certification ladder on its own; a harmful claim about a named person still
    needs real tier-1 confirmation no matter how viral it gets.
- **Certification badge**, queryable per claim/story:
  `unconfirmed → corroborated → primary-source-verified → disputed/retracted`.
  Surfaced in the curation UI, but stored as structured data, not just UI text
  — this matters for §6.
- **Evidence snapshotting**: permanent archive of cited evidence (screenshot +
  text hash on ingest) so a story's citations can't quietly change or vanish.
- **Media forensics on `MediaAsset`**: reverse-image search + EXIF check before
  anything ships with a photo.

---

## 4. Geography / globe expansion (Phase 4)

You already have the bones: `EventGeometry`, `EventLayer`, `Event`, canonical
entities with lat/lon. Mostly UI + data-feeding work once Phases 1–3 exist.

- Sensor-fed live events from §1a's ADS-B/AIS/USGS feeds flow into `events`
  directly — no journalist required for a quake or wildfire to show up.
- Historical playback (scrub timeline, not just current state).
- Layer types beyond points: conflict-zone polygons, disputed-territory
  overlays, disaster radii.
- Sub-national resolution — region/admin-boundary geocoding instead of just
  country/city, which makes conflict tracking actually useful on the globe.

---

## 5. Cross-cutting: the certification badge is a stronger product than the badge display

Worth keeping in view while building Phases 1–2: the certified claim graph
itself — structured, queryable, with receipts — is a stronger sellable product
than "a tool that helps you post to social." An API answering "what's actually
been verified about X, and by whom" is worth paying for; a repost helper isn't.
This is why §2/§3's tables are designed to be clean and queryable on their own,
not just good enough to render a UI badge.

---

## 6. Budget checkpoints

"Ingested constantly, everything imaginable" is in direct tension with
Supabase's 500MB free tier, Groq's 1K req/day, and GitHub Actions minutes.
Rough checkpoints, not a hard schedule:

- **Stays free indefinitely**: RSS ingestion, sensor feeds (most are keyless
  public data — USGS, GDELT), entity/geo processing, the claim/edge tables
  themselves (structured text, small).
- **Needs sampling or a small budget eventually**: STT on podcasts, image
  forensics at scale, translation at scale, OSINT firehose volume once it's
  more than a trickle.
- **Trigger for spending**: once §5's certified graph is good enough that you'd
  pay for access to it yourself, that's the signal to start monetizing rather
  than waiting for a "done" that a $0 budget can't reach for a project this
  size.

---

## 7. Open questions (not yet decided)

- Exact harm-level classifier for the dynamic gate — heuristic/keyword-based
  first, or LLM-classified from the start?
- Where leaked/whistleblower datasets slot into the tier system — new tier-5,
  or a separate manual-curation path that never auto-promotes?
- How aggressively to backfill `DATA_SOURCES.md` for the *existing* tier-1/2/3
  sources before adding new ones.