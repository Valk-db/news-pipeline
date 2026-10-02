"""Analyzer versions and input hashes: the contract that makes derived state recomputable.

`raw_articles` is the immutable raw layer and everything downstream of it is derived, but until
now a derived row carried no record of *what produced it*. Fixing the dedupe or the clustering
code therefore had exactly one remedy available: re-ingest everything, because nothing could
tell the pipeline which existing rows the change affected.

Two columns on every derived table close that gap, and they answer different questions:

* `analyzer_version` -- which analyzer produced this row. Bump the constant in this file when
  that analyzer's *logic* changes, and every row written by the old logic becomes identifiable
  without guessing. This is the contract: a version bump is the signal that a stage's output
  is stale, and `scripts/recompute_derived.py` deletes exactly the rows whose version no longer
  matches.
* `input_hash` -- SHA256 over the canonical input that row was computed from (the member
  article ids of a reporting unit, the sorted unit ids of a story, the text an embedding was
  computed over). Two runs over the same input produce the same hash, so recomputation is
  verifiable rather than merely repeatable, and an upstream change to a single member is
  visible as a single changed hash.

Both are nullable, and they are nullable on purpose: every row that existed before migration
20261002050000 has them NULL. NULL therefore means "written before this was recorded" -- which
for `scripts/recompute_derived.py --stage X` is *stale*, since it cannot be shown to have been
produced by the current analyzer. A row whose analyzer_version equals the current constant is
the only kind this design trusts.

Two properties are load-bearing:

* **analyzer_version names the stage that CREATED the row, not the last one to touch it.**
  `canonical_entities` is created by entity canonicalization (`cluster/v1`) and then mutated in
  place by the geocoder; the row keeps `cluster/v1`, because that is what decides whether the
  row itself is stale. The geocoder's effect on a row is recorded by recording it on the row
  the geocoder determines -- `events`, which carries `geocode/v1` because an Event's only
  computed content is a point that came out of the geocoder.
* **A version bump is the only thing that marks rows stale.** Tuning a threshold or a prompt
  without bumping the constant leaves the rows looking current. Where a numeric or textual
  setting genuinely changes a row's content it is folded into that row's `input_hash` instead
  (see `reporting_units`, whose hash covers the containment threshold), so the change shows up
  in the hash rather than needing a bump.

The hashing primitives below are deliberately generic. Stage-specific knowledge (which ids
matter, in what order) belongs at the write point in the stage that owns it, so a stage's hash
can be read next to the code that computed it; this module owns the framing, the constants,
and the table/stage registry that the recompute script walks.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

# =============================================================================
# Version constants. One per analyzer. Bump on a logic change, never on a deploy.
#
# The value is "<stage>/<major>": the stage name is the identity, the number is the revision of
# that analyzer. Two-digit padding is not required, but keeping the numbers ordered inside the
# string means a lexicographic sort of the column is also a chronological sort of the versions.
# =============================================================================

# Near-duplicate grouping into reporting units (src/verification/units.py), and the
# article/article SAME_EVENT_AS edges the ingest writes when two feeds carry one story.
DEDUPE_VERSION = "dedupe/v1"

# Entity canonicalization (src/utils/ner.py): surface forms clustered onto canonical ids,
# plus the alias rows that make a canonical entity resolvable.
CLUSTER_VERSION = "cluster/v1"

# Story grouping (src/verification/stories.py): which reporting units cover one event, the
# story row and the story_unit_links edges.
STORY_VERSION = "story/v1"

# Viewpoint sub-clustering (cluster_viewpoints() in the same module): the child stories that
# split one event by stance. Separate from STORY_VERSION because the sub-clustering is a
# different analyzer -- an LLM prompt over unit excerpts -- and because its idempotency guard
# (skip a parent that already has children) makes a stale child stick rather than duplicate.
VIEWPOINT_VERSION = "viewpoint/v1"

# The geocoder (src/enrichment/geocoder.py) and the rows whose content is a geocoded point:
# `events`. Bumping this says "every point in the database may now be wrong", which is a
# different and much more expensive statement than bumping any other constant here.
GEOCODE_VERSION = "geocode/v1"

# Vector embeddings (src/enrichment/embedding_service.py and the writers that persist them).
# The embedding model name is already its own column; this version covers the pipeline logic
# around it -- which texts are combined, in what order, how they are truncated.
EMBEDDING_VERSION = "embedding/v1"

# Claim extraction (src/verification/claims.py): the claims and the per-unit evidence matrix.
CLAIM_VERSION = "claim/v1"

# Topic assignment (src/verification/topics.py).
TOPIC_VERSION = "topic/v1"

# Narrative arcs (src/verification/narrative.py): the story/story entity_edges.
NARRATIVE_VERSION = "narrative/v1"

# Daily source reliability scoring (src/reliability/consensus_analyzer.py).
RELIABILITY_VERSION = "reliability/v1"

# The corroboration gate (src/verification/tiers.py): every decision it makes, appended to
# gate_decisions. Bumping this says "a decision row written before this bump was made under
# different counting rules". v2 is the wire-collapse counting change, and it counts in both gates:
# the boolean gate and the admission score both now measure independence post wire-collapse, so
# v1 rows were counted with the syndicated-copies bug in one of the two and are not comparable
# with v2 rows. The ledger is append-only, so a v1 row stays exactly as it was written and reads
# under its own version -- re-gating a v1 story appends a v2 row beside it instead of rewriting it.
GATE_VERSION = "gate/v2"


# =============================================================================
# Table and stage registry.
#
# DERIVED_TABLES is the complete list of tables that carry analyzer_version/input_hash. It is
# the list the migration creates the columns on, the list a drift check asserts against the
# models, and the list the recompute script walks -- one definition, three consumers, so the
# three cannot disagree.
#
# TABLES_WITHOUT_PRODUCER is the honest remainder: tables that are derived state in intent but
# have no writer in this codebase, so their columns exist and stay NULL. Listing them beats
# leaving them out, because "no producer" is then a fact someone can check instead of a gap
# someone has to discover.
# =============================================================================

DERIVED_TABLES: tuple[str, ...] = (
    "reporting_units",
    "stories",
    "story_unit_links",
    "canonical_entities",
    "entity_aliases",
    "event_geometries",
    "events",
    "article_embeddings",
    "story_embeddings",
    "claims",
    "claim_evidence",
    "entity_edges",
    "story_topic_groups",
    "source_reliability_snapshots",
    "gate_decisions",
)

TABLES_WITHOUT_PRODUCER: tuple[str, ...] = (
    # Nothing constructs an EventGeometry. The table and its RLS policy exist from the
    # globe-schema migration and every read path that mentions it (curation_ui/events.py) is
    # reading `Event`. There is no writer to stamp, so both columns stay NULL.
    "event_geometries",
    # embedding_service.embed_article() returns the payload an ArticleEmbedding row would
    # need, but no caller persists it: the only embedding writer is
    # enrichment/pipeline._persist_story_embedding. Same situation, same answer.
    "article_embeddings",
)

# The third way a derived table can be unaccounted for, and the reason it is listed rather than
# left implicit: these tables carry the columns and have a writer, but
# scripts/recompute_derived.py must never delete from them.
#
#   gate_decisions -- append-only ledger. "Recomputing" a decision is re-running the gate, which
#     appends the corrected decision and keeps the superseded one readable; deleting the rows
#     first would destroy exactly the history the table exists to hold, and rewriting them in
#     place would be the UPDATE path the INSERT-only convention forbids. So it carries
#     analyzer_version for provenance and is absent from STAGE_TABLES by design.
NOT_RECOMPUTED_TABLES: tuple[str, ...] = (
    "gate_decisions",
)

# Every analyzer_version value legitimately found on each derived table, in preference order.
# Most tables have exactly one writer, so the value is just the stage's version. Two tables have
# more than one and that is a fact about the pipeline, not an oversight to paper over:
#
#   stories       -- create_story_from_units writes STORY_VERSION; cluster_viewpoints writes a
#                    child story per viewpoint with VIEWPOINT_VERSION. Both are "story rows",
#                    and a story bump has to reach both, so a row is current if it matches either.
#   entity_edges  -- narrative.link_narrative_arcs writes STORY_VERSION subject->object arcs;
#                    ingestion/rss_evidence.link_to_gdelt_radar writes article->article
#                    SAME_EVENT_AS dedupe edges. Two unrelated edges in one table.
#
# This map is the single source: TABLE_VERSION below is derived from it, and it is what the
# recompute script compares against, so "stale" is defined once for the auditor and once for the
# deleter. Every table in DERIVED_TABLES must appear here or in TABLES_WITHOUT_PRODUCER;
# assert_all_covered() is the test that keeps that true.
TABLE_VERSIONS: dict[str, tuple[str, ...]] = {
    "reporting_units": (DEDUPE_VERSION,),
    "stories": (STORY_VERSION, VIEWPOINT_VERSION),
    "story_unit_links": (STORY_VERSION, VIEWPOINT_VERSION),
    "canonical_entities": (CLUSTER_VERSION,),
    "entity_aliases": (CLUSTER_VERSION,),
    "events": (GEOCODE_VERSION,),
    "story_embeddings": (EMBEDDING_VERSION,),
    "claims": (CLAIM_VERSION,),
    "claim_evidence": (CLAIM_VERSION,),
    "entity_edges": (NARRATIVE_VERSION, DEDUPE_VERSION),
    "story_topic_groups": (TOPIC_VERSION,),
    "source_reliability_snapshots": (RELIABILITY_VERSION,),
    "gate_decisions": (GATE_VERSION,),
}

# The primary (first-listed) version per table. Use this when you need a single label, e.g. to
# say which analyzer a stage "is". Use TABLE_VERSIONS when you need to decide staleness.
TABLE_VERSION: dict[str, str] = {t: v[0] for t, v in TABLE_VERSIONS.items()}


def expected_versions(table: str) -> tuple[str, ...]:
    """The analyzer_version values that count as current on `table`.

    A row is stale when its analyzer_version is NULL -- it predates this bookkeeping, so we do
    not know what produced it and cannot claim it is current -- or when it is none of these.
    """
    try:
        return TABLE_VERSIONS[table]
    except KeyError:
        raise KeyError(
            f"{table!r} is not a derived table; known: {', '.join(sorted(TABLE_VERSIONS))}"
        ) from None

# The tables each recomputable stage owns. Ordered by dependency: a stage may only be re-run
# once every stage before it in this tuple has been, because each one reads the previous one's
# output. scripts/recompute_derived.py deletes a stage and everything after it, so this order
# is also the delete order (reversed) and the reason `--include-downstream` can walk it.
STAGE_ORDER: tuple[str, ...] = (
    "dedupe",
    "cluster",
    "stories",
    "geocode",
    "embeddings",
    "topics",
    "narrative",
    "claims",
    "reliability",
)

STAGE_TABLES: dict[str, tuple[str, ...]] = {
    "dedupe": ("reporting_units",),
    "cluster": ("canonical_entities", "entity_aliases"),
    "stories": ("stories", "story_unit_links"),
    "geocode": ("events",),
    "embeddings": ("story_embeddings",),
    "topics": ("story_topic_groups",),
    "narrative": ("entity_edges",),
    "claims": ("claims", "claim_evidence"),
    "reliability": ("source_reliability_snapshots",),
}

# Spellings people reach for, mapped onto the stage that owns the work. The pipeline phase in
# src/ingestion/run.py is called "ingest"/"reporting_units"/"stories"; an operator asking for
# `--stage units` means the dedupe stage, not a stage that exists.
STAGE_ALIASES: dict[str, str] = {
    "units": "dedupe",
    "reporting_units": "dedupe",
    "entities": "cluster",
    "entity": "cluster",
    "ner": "cluster",
    "story": "stories",
    "viewpoint": "stories",
    "geo": "geocode",
    "events": "geocode",
    "embed": "embeddings",
    "embedding": "embeddings",
    "claim": "claims",
    "topic": "topics",
    "reliability": "reliability",
}


def resolve_stage(name: str) -> str:
    """Canonical stage key for a user-supplied name, or a ValueError naming the real ones."""
    key = (name or "").strip().lower()
    key = STAGE_ALIASES.get(key, key)
    if key not in STAGE_TABLES:
        raise ValueError(
            f"unknown stage {name!r}; known stages: {', '.join(STAGE_ORDER)}"
        )
    return key


def stage_version(stage: str) -> str:
    """The version constant a stage's rows carry."""
    return TABLE_VERSION[STAGE_TABLES[resolve_stage(stage)][0]]


def downstream_stages(stage: str) -> tuple[str, ...]:
    """Stages after `stage` in dependency order, i.e. everything computed from its output."""
    key = resolve_stage(stage)
    return STAGE_ORDER[STAGE_ORDER.index(key) + 1:]


def downstream_tables(stage: str) -> tuple[str, ...]:
    """Every derived table owned by `stage` or by a stage downstream of it, in delete order."""
    affected = (resolve_stage(stage), *downstream_stages(stage))
    tables: list[str] = []
    for key in reversed(affected):  # downstream first: FK targets before FK sources
        tables.extend(STAGE_TABLES[key])
    return tuple(tables)


def assert_all_covered() -> None:
    """Every derived table must have a version or be declared producer-less or not-recomputed."""
    declared = set(TABLE_VERSION) | set(TABLES_WITHOUT_PRODUCER) | set(NOT_RECOMPUTED_TABLES)
    missing = sorted(set(DERIVED_TABLES) - declared)
    extra = sorted(declared - set(DERIVED_TABLES))
    if missing or extra:
        raise AssertionError(
            f"DERIVED_TABLES disagrees with TABLE_VERSION/TABLES_WITHOUT_PRODUCER/"
            f"NOT_RECOMPUTED_TABLES: undeclared={missing} unlisted={extra}"
        )
    for stage, tables in STAGE_TABLES.items():
        if stage not in STAGE_ORDER:
            raise AssertionError(f"{stage} is missing from STAGE_ORDER")
        for table in tables:
            if table not in TABLE_VERSION:
                raise AssertionError(f"stage {stage} owns {table}, which has no analyzer version")
    for alias, stage in STAGE_ALIASES.items():
        if stage not in STAGE_TABLES:
            raise AssertionError(f"alias {alias!r} points at unknown stage {stage!r}")


# =============================================================================
# Hashing primitives.
#
# The framing has to be exact, not merely conventional: a payload whose parts run together
# ("AB" + "C" vs "A" + "BC") would make two different inputs hash the same. Each part is
# therefore length-prefixed before it is folded in, so no part's contents can imitate another's
# boundary, and the stage version is folded in as the first part so a version bump changes
# every hash it would otherwise have produced.
#
# Lists render in the order given -- the caller decides what "canonical order" means, because
# it differs by stage (member unit ids are sorted, article ids within a unit are sorted, but the
# order of texts folded into a story embedding is meaningful and must not be normalized away).
# =============================================================================

def render_for_hash(part: object) -> str:
    """Deterministic text rendering of one hash part.

    UUIDs, datetimes and enums go through str(); None is its own token so a null input cannot
    be confused with the empty string; mappings are emitted with their keys sorted, because a
    JSON-ish field (Story.primary_entities as a dict, Event.entities) has no inherent order.
    """
    if part is None:
        return "\x00none"
    if isinstance(part, bool):
        return "true" if part else "false"
    if isinstance(part, (list, tuple)):
        return "\x1e".join(render_for_hash(item) for item in part)
    if isinstance(part, (set, frozenset)):
        # A set has no order, so it cannot be hashed as given. Sorted, or the same members
        # would produce a different digest on every process.
        return "\x1e".join(render_for_hash(item) for item in sorted(part, key=str))
    if isinstance(part, dict):
        return "\x1e".join(f"{key}={render_for_hash(part[key])}" for key in sorted(part, key=str))
    return str(part)


def compute_input_hash(stage_version: str, *parts: object) -> str:
    """SHA256 hex over the stage version and the canonical input `parts`.

    `stage_version` is part of the payload by design: the same input analyzed by a different
    analyzer version is a different result, and its hash should say so without anyone having to
    remember to bump a separate field.
    """
    digest = hashlib.sha256()
    for part in (f"analyzer_version={stage_version}", *parts):
        chunk = render_for_hash(part).encode("utf-8")
        digest.update(len(chunk).to_bytes(8, "big"))
        digest.update(chunk)
    return digest.hexdigest()


def hash_id_set(stage_version: str, ids: Iterable[object]) -> str:
    """Hash of a membership set (the member articles of a unit, the member units of a story).

    Sorted and de-duplicated: a set is the same set however it was assembled, and two runs that
    built it in a different order must produce the same digest or recomputation would look like
    a change.
    """
    return compute_input_hash(stage_version, sorted({str(i) for i in ids}))


def hash_text(stage_version: str, *fields: object) -> str:
    """Hash of free text, with whitespace collapsed and case folded first.

    Used where the input is text a human reads (claim text, the text an embedding was computed
    over): two runs that differ only in re-wrapped whitespace or capitalization are computing
    the same thing, and their hashes should agree.
    """
    normalized = []
    for field in fields:
        text = "" if field is None else str(field)
        normalized.append(" ".join(text.split()).casefold())
    return compute_input_hash(stage_version, *normalized)