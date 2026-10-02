"""Recomputable derived state: analyzer_version + input_hash.

Three claims, one per section:

1. Every derived table has the columns, and raw_articles does not.
2. Every stage that writes derived state stamps both columns with a value that can be
   recomputed from the inputs the test set up -- not just "some string".
3. The recompute script's staleness predicate, table order and blocker detection agree with
   the schema and the version registry.

Section 1 also runs the migration itself against a scratch Postgres schema, which is the only
way to check the DDL rather than the Python that describes it. It skips without DATABASE_URL.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from scripts.recompute_derived import (
    audit_table,
    delete_order,
    model_by_table,
    reference_pairs,
    stale_clause,
)
from src.schema.models import (
    CanonicalEntity,
    Claim,
    ClaimEvidence,
    EdgePredicate,
    EntityAlias,
    EntityEdge,
    Event,
    RawArticle,
    ReportingUnit,
    SourceReliabilitySnapshot,
    SourceTier,
    Story,
    StoryEmbedding,
    StoryTopicGroup,
    StoryUnitLink,
    TopicGroup,
)
from src.shared.analyzer_versions import (
    CLAIM_VERSION,
    EMBEDDING_VERSION,
    CLUSTER_VERSION,
    DEDUPE_VERSION,
    DERIVED_TABLES,
    GEOCODE_VERSION,
    NARRATIVE_VERSION,
    NOT_RECOMPUTED_TABLES,
    RELIABILITY_VERSION,
    STAGE_ORDER,
    STAGE_TABLES,
    STORY_VERSION,
    TOPIC_VERSION,
    VIEWPOINT_VERSION,
    assert_all_covered,
    compute_input_hash,
    downstream_stages,
    downstream_tables,
    expected_versions,
    hash_id_set,
    hash_text,
    resolve_stage,
    stage_version,
)
from src.verification.claims import extract_claims_for_story
from src.verification.narrative import link_narrative_arcs
from src.verification.stories import build_stories
from src.verification.topics import FALLBACK_GROUP, assign_story_topic_groups
from src.verification.units import build_reporting_units

# =============================================================================
# 1. The columns exist where they should, and only there.
# =============================================================================


def test_every_derived_table_declares_both_columns():
    assert_all_covered()
    for table in DERIVED_TABLES:
        model = model_by_table()[table]
        assert hasattr(model, "analyzer_version"), table
        assert hasattr(model, "input_hash"), table
        for name in ("analyzer_version", "input_hash"):
            column = model.__table__.c[name]
            assert column.nullable, f"{table}.{name} must be nullable: existing rows predate it"
            assert column.type.length == 64, f"{table}.{name} holds a SHA256 hex digest"


def test_raw_articles_is_the_immutable_layer_and_has_neither_column():
    model = model_by_table()["raw_articles"]
    assert not hasattr(model, "analyzer_version")
    assert not hasattr(model, "input_hash")


def test_the_registry_and_the_stage_tables_agree():
    assert set(STAGE_ORDER) == set(STAGE_TABLES)
    covered = {t for tables in STAGE_TABLES.values() for t in tables}
    # event_geometries and article_embeddings have no writer, and gate_decisions is written by a
    # gate run rather than by a recompute stage -- it is an append-only ledger, never recomputed.
    absent = {"event_geometries", "article_embeddings", *NOT_RECOMPUTED_TABLES}
    assert covered | absent == set(DERIVED_TABLES)
    for stage, tables in STAGE_TABLES.items():
        for table in tables:
            assert stage_version(stage) in expected_versions(table), f"{stage} -> {table}"


def test_downstream_is_a_suffix_of_the_dependency_order():
    for stage in STAGE_ORDER:
        downstream = downstream_stages(stage)
        assert list(downstream) == list(STAGE_ORDER[STAGE_ORDER.index(stage) + 1 :])
        # A stage is always part of its own blast radius.
        assert set(downstream_tables(stage)) >= set(STAGE_TABLES[stage])


def test_every_stage_and_its_aliases_resolve():
    for stage in STAGE_ORDER:
        assert resolve_stage(stage.upper()) == stage
    for alias, stage in [("units", "dedupe"), ("ner", "cluster"), ("viewpoint", "stories")]:
        assert resolve_stage(alias) == stage
    with pytest.raises(ValueError, match="known stages"):
        resolve_stage("nonsense")


# =============================================================================
# 2. Stages stamp both columns, reproducibly.
# =============================================================================


def _article(session, domain="apnews.com", url_id="a", entities=None, body=None, tier=SourceTier.TIER1):
    now = datetime.now(timezone.utc)
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://{domain}/{url_id}",
        url_hash=f"hash-{url_id}",
        title=f"Title {url_id}",
        body_text=body or f"Body text for {url_id} with enough words to shingle into clusters properly.",
        source_domain=domain,
        source_tier=tier,
        published_at=now,
        entities=entities or {},
    )
    session.add(article)
    return article


async def _unit(session, domain="apnews.com", url_id="u", entities=None):
    article = _article(session, domain=domain, url_id=url_id, entities=entities)
    unit = ReportingUnit(
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=article.id,
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    session.add(unit)
    await session.flush()
    article.reporting_unit_id = unit.id
    return unit


async def test_dedupe_stamps_the_unit_with_its_membership(db_session):
    body = "Officials confirmed the details of the event to reporters in the capital city today."
    ids = []
    for n in ("1", "2"):
        article = _article(db_session, url_id=f"dup{n}", body=body)
        ids.append(str(article.id))
    await db_session.commit()

    assert await build_reporting_units(db_session) == 1

    unit = (await db_session.execute(select(ReportingUnit))).scalar_one()
    assert unit.analyzer_version == DEDUPE_VERSION
    # Same membership in a different order is the same input, so the hash must not depend
    # on which article the loop happened to reach first.
    assert unit.input_hash == compute_input_hash(DEDUPE_VERSION, sorted(ids), 0.9)
    assert unit.input_hash == compute_input_hash(DEDUPE_VERSION, sorted(reversed(ids)), 0.9)
    assert len(unit.input_hash) == 64


async def test_dedupe_folds_the_threshold_into_the_hash(db_session):
    """A tuning change is an input change, so it shows up without a version bump."""
    from src.shared.analyzer_versions import compute_input_hash as h

    article = _article(db_session, url_id="one")
    await db_session.commit()
    await build_reporting_units(db_session)
    unit = (await db_session.execute(select(ReportingUnit))).scalar_one()

    assert unit.input_hash == h(DEDUPE_VERSION, [str(article.id)], 0.9)
    assert unit.input_hash != h(DEDUPE_VERSION, [str(article.id)], 0.5)


async def test_stories_stamp_story_links_and_canonical_entities(db_session):
    entities = {"PERSON": ["John Smith"], "GPE": ["Washington"], "ORG": ["White House"]}
    other = {"PERSON": ["Jane Doe"], "GPE": ["London"], "ORG": ["Bank of England"]}
    unit_a = await _unit(db_session, domain="apnews.com", url_id="s1", entities=entities)
    unit_b = await _unit(db_session, domain="reuters.com", url_id="s2", entities=other)
    await db_session.commit()

    modified = await build_stories(db_session)
    assert len(modified) == 2

    stories = (await db_session.execute(select(Story))).scalars().all()
    links = (await db_session.execute(select(StoryUnitLink))).scalars().all()
    assert len(links) == 2

    story = next(
        s for s in stories if any(link.story_id == s.id and link.unit_id == unit_a.id for link in links)
    )
    assert story.analyzer_version == STORY_VERSION
    assert story.input_hash == hash_id_set(STORY_VERSION, [unit_a.id])

    other = next(s for s in stories if s.id != story.id)
    assert other.input_hash == hash_id_set(STORY_VERSION, [unit_b.id])
    assert other.input_hash != story.input_hash

    for link in links:
        assert link.analyzer_version == STORY_VERSION
        assert link.input_hash == compute_input_hash(STORY_VERSION, str(link.story_id), str(link.unit_id))

    canonical = (await db_session.execute(select(CanonicalEntity))).scalars().all()
    aliases = (await db_session.execute(select(EntityAlias))).scalars().all()
    assert canonical and aliases, "build_stories resolves entities, so it writes both"
    for row in canonical:
        assert row.analyzer_version == CLUSTER_VERSION
        assert row.input_hash == compute_input_hash(CLUSTER_VERSION, row.entity_type, row.canonical_name)
    for row in aliases:
        assert row.analyzer_version == CLUSTER_VERSION
        assert row.input_hash == compute_input_hash(CLUSTER_VERSION, row.alias)


async def test_claims_stamp_claim_and_evidence(db_session):
    unit_a = await _unit(db_session, url_id="c1")
    unit_b = await _unit(db_session, domain="bbc.com", url_id="c2")
    story = Story(
        day=datetime.now(timezone.utc),
        primary_entities=["Gov"],
        tier1_unit_count=2,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=1,
        status=Story.Status.QUEUED,
    )
    db_session.add(story)
    await db_session.flush()
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit_a.id))
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit_b.id))
    await db_session.commit()

    payload = {
        "claims": [
            {
                "text": "The government announced new policies.",
                "claim_type": "fact",
                "evidence": [
                    {"unit_id": str(unit_a.id), "stance": "supports", "confidence": 90},
                    {"unit_id": str(unit_b.id), "stance": "disputes", "confidence": 40},
                ],
            }
        ]
    }
    llm = MagicMock()
    llm.chat_completion = AsyncMock(
        return_value={"choices": [{"message": {"content": "{}"}}]}
    )
    llm._parse_json_response = MagicMock(return_value=payload)

    with patch("src.shared.llm.get_llm_client", AsyncMock(return_value=llm)):
        result = await extract_claims_for_story(db_session, story.id)

    assert result["claims_created"] == 1 and result["errors"] == []

    claim = (await db_session.execute(select(Claim))).scalar_one()
    assert claim.analyzer_version == CLAIM_VERSION
    # Claim text is whitespace/case-normalized, so a re-wrapped or re-cased claim hashes the same.
    assert claim.input_hash == hash_text(
        CLAIM_VERSION, story.id, "The government announced new policies."
    )
    assert claim.input_hash == hash_text(
        CLAIM_VERSION, story.id, "  the GOVERNMENT announced\nnew policies. "
    )
    assert claim.input_hash != hash_text(CLAIM_VERSION, story.id, "A different claim.")

    evidence = (await db_session.execute(select(ClaimEvidence))).scalars().all()
    assert len(evidence) == 2
    for row in evidence:
        assert row.analyzer_version == CLAIM_VERSION
        assert row.input_hash == compute_input_hash(
            CLAIM_VERSION, str(claim.id), str(row.unit_id), row.stance.value
        )
    # Confidence is a judgement about the stance, not an input to it.
    assert evidence[0].input_hash != evidence[1].input_hash
    assert evidence[0].confidence != evidence[1].confidence


async def test_topics_stamp_the_matched_assignment(db_session):
    from src.verification.topics import TOPIC_KEYWORDS

    group_name = next(iter(TOPIC_KEYWORDS))
    keyword = TOPIC_KEYWORDS[group_name][0]
    group = TopicGroup(name=group_name)
    db_session.add(group)
    story = Story(
        day=datetime.now(timezone.utc),
        primary_entities=[keyword.capitalize()],
        status=Story.Status.QUEUED,
    )
    db_session.add(story)
    await db_session.commit()

    results = await assign_story_topic_groups(db_session, story.id)
    assert [r["topic_group"] for r in results] == [group_name]
    assert results[0]["created"]

    assignment = (await db_session.execute(select(StoryTopicGroup))).scalar_one()
    assert assignment.analyzer_version == TOPIC_VERSION
    assert assignment.input_hash == compute_input_hash(
        TOPIC_VERSION, str(story.id), str(group.id)
    )


async def test_topics_stamp_the_fallback_assignment(db_session):
    """The other write point: nothing matched, so the story lands in the generic group."""
    group = TopicGroup(name=FALLBACK_GROUP)
    db_session.add(group)
    story = Story(
        day=datetime.now(timezone.utc),
        primary_entities=["Zzzqqq"],
        status=Story.Status.QUEUED,
    )
    db_session.add(story)
    await db_session.commit()

    results = await assign_story_topic_groups(db_session, story.id)
    assert [r["topic_group"] for r in results] == [FALLBACK_GROUP]

    assignment = (await db_session.execute(select(StoryTopicGroup))).scalar_one()
    assert assignment.analyzer_version == TOPIC_VERSION
    assert assignment.input_hash == compute_input_hash(
        TOPIC_VERSION, str(story.id), str(group.id)
    )


async def test_narrative_stamps_the_story_to_story_edge(db_session):
    day = datetime.now(timezone.utc)
    older = Story(day=day - timedelta(days=3), primary_entities=["Israel"], status=Story.Status.QUEUED)
    newer = Story(day=day, primary_entities=["Israel", "Gaza"], status=Story.Status.QUEUED)
    db_session.add_all([older, newer])
    await db_session.commit()

    results = await link_narrative_arcs(db_session, newer.id)
    assert results, f"no arc linked: {results}"

    edges = (await db_session.execute(select(EntityEdge))).scalars().all()
    assert edges
    for edge in edges:
        assert edge.analyzer_version == NARRATIVE_VERSION
        assert edge.input_hash == compute_input_hash(
            NARRATIVE_VERSION,
            edge.subject_type,
            str(edge.subject_id),
            edge.predicate.value,
            edge.object_type,
            str(edge.object_id),
        )


async def test_reliability_stamps_the_snapshot(db_session):
    from src.reliability.consensus_analyzer import compute_daily_reliability_snapshots

    date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    db_session.add(
        RawArticle(
            id=uuid.uuid4(),
            url="https://apnews.com/r1",
            url_hash="r1",
            title="t",
            body_text="b",
            source_domain="apnews.com",
            source_tier=SourceTier.TIER1,
            published_at=date - timedelta(days=1),
        )
    )
    await db_session.commit()

    count = await compute_daily_reliability_snapshots(db_session, date=date)
    if count == 0:
        pytest.skip("no source passed the 30-day window in this fixture")

    snapshot = (await db_session.execute(select(SourceReliabilitySnapshot))).scalar_one()
    assert snapshot.analyzer_version == RELIABILITY_VERSION
    # One snapshot is one (source, day) scoring run, so that pair is its input. The date the
    # stage hashed is the one it was handed, not what SQLite round-tripped back.
    assert snapshot.input_hash == compute_input_hash(
        RELIABILITY_VERSION, snapshot.source_domain, date.isoformat()
    )


async def test_events_stamp_the_geocoded_point():
    """No DB needed: story_event() builds the row in memory from story + entity + point."""
    from scripts.backfill_globe_events import story_event

    story = Story(
        id=uuid.uuid4(), day=datetime.now(timezone.utc),
        primary_entities=[], status=Story.Status.QUEUED,
    )
    entity = CanonicalEntity(
        id=uuid.uuid4(),
        entity_type="GPE",
        canonical_name="Jerusalem",
        latitude=31.7683,
        longitude=35.2137,
    )
    event = story_event(story, entity, layer_id=None)
    assert isinstance(event, Event)
    assert event.analyzer_version == GEOCODE_VERSION
    assert event.input_hash == compute_input_hash(
        GEOCODE_VERSION, str(story.id), "Jerusalem", 31.7683, 35.2137
    )


async def test_story_embedding_stamps_the_text_it_embedded(db_session):
    from src.enrichment.pipeline import _persist_story_embedding

    story_id = uuid.uuid4()
    await _persist_story_embedding(
        db_session,
        story_id,
        {
            "model": "test-model",
            "embedding": [0.1, 0.2],
            "dimensions": 2,
            "embedded_text_sha256": "f" * 64,
        },
    )
    await db_session.commit()

    row = (await db_session.execute(select(StoryEmbedding))).scalar_one()
    assert row.analyzer_version == EMBEDDING_VERSION
    assert row.input_hash == compute_input_hash(
        EMBEDDING_VERSION, str(story_id), "test-model", "f" * 64
    )
    # A different model is a different vector, even from identical text.
    assert row.input_hash != compute_input_hash(
        EMBEDDING_VERSION, str(story_id), "other-model", "f" * 64
    )


async def test_story_embedding_without_a_text_digest_records_no_hash(db_session):
    """A payload built without embedded_text_sha256 must not claim it knows the input."""
    from src.enrichment.pipeline import _persist_story_embedding

    await _persist_story_embedding(
        db_session, uuid.uuid4(), {"model": "m", "embedding": [0.1], "dimensions": 1}
    )
    await db_session.commit()

    row = (await db_session.execute(select(StoryEmbedding))).scalar_one()
    assert row.analyzer_version == EMBEDDING_VERSION
    assert row.input_hash is None


async def test_embed_story_digests_exactly_what_it_sent():
    """The recorded digest has to be of the string the model saw, including the 10-article cut."""
    import hashlib

    from src.enrichment.embedding_service import embed_story

    service = MagicMock()
    service.model_name = "test-model"
    service.dimensions = 2
    service.generate_embedding = AsyncMock(return_value=[0.1, 0.2])

    with patch("src.enrichment.embedding_service.get_embedding_service", return_value=service):
        result = await embed_story("s1", [f"text {n}" for n in range(12)])

    service.generate_embedding.assert_awaited_once_with(
        " ".join(f"text {n}" for n in range(10))
    )
    assert result["embedded_text_sha256"] == hashlib.sha256(
        " ".join(f"text {n}" for n in range(10)).encode("utf-8")
    ).hexdigest()


# =============================================================================
# 3. The recompute script picks the right rows.
# =============================================================================


async def test_stale_is_null_or_not_a_current_version(db_session):
    story = Story(
        id=uuid.uuid4(), day=datetime.now(timezone.utc),
        primary_entities=[], status=Story.Status.QUEUED,
    )
    db_session.add(story)
    await db_session.flush()

    current = Story(
        id=uuid.uuid4(), day=datetime.now(timezone.utc), primary_entities=[],
        status=Story.Status.QUEUED, analyzer_version=STORY_VERSION, input_hash="a" * 64,
    )
    viewpoint = Story(
        id=uuid.uuid4(), day=datetime.now(timezone.utc), primary_entities=[],
        status=Story.Status.QUEUED, analyzer_version=VIEWPOINT_VERSION, input_hash="b" * 64,
    )
    old = Story(
        id=uuid.uuid4(), day=datetime.now(timezone.utc), primary_entities=[],
        status=Story.Status.QUEUED, analyzer_version="story/v0", input_hash="c" * 64,
    )
    db_session.add_all([current, viewpoint, old])
    await db_session.commit()

    found = (
        await db_session.execute(select(Story).where(stale_clause("stories")))
    ).scalars().all()

    assert {s.id for s in found} == {story.id, old.id}, (
        "NULL counts as stale (unaccounted for) and an old version counts as stale; "
        "both current writers' versions must be spared"
    )


async def test_edges_from_both_writers_count_as_current(db_session):
    """entity_edges has two writers; a dedupe edge is not stale narrative state."""
    story_id = uuid.uuid4()
    db_session.add_all([
        EntityEdge(subject_type="story", subject_id=story_id,
                   predicate=EdgePredicate.PART_OF_NARRATIVE,
                   object_type="story", object_id=uuid.uuid4(),
                   analyzer_version=NARRATIVE_VERSION),
        EntityEdge(subject_type="article", subject_id=story_id,
                   predicate=EdgePredicate.SAME_EVENT_AS,
                   object_type="article", object_id=uuid.uuid4(),
                   analyzer_version=DEDUPE_VERSION),
    ])
    await db_session.commit()

    stale = (
        await db_session.execute(select(EntityEdge).where(stale_clause("entity_edges")))
    ).scalars().all()
    assert stale == []


async def test_audit_counts_current_and_stale_separately(db_session):
    db_session.add_all([
        Story(id=uuid.uuid4(), day=datetime.now(timezone.utc), primary_entities=[],
              status=Story.Status.QUEUED, analyzer_version=STORY_VERSION, input_hash="a" * 64),
        Story(id=uuid.uuid4(), day=datetime.now(timezone.utc), primary_entities=[],
              status=Story.Status.QUEUED, analyzer_version=STORY_VERSION, input_hash="a" * 64),
        Story(id=uuid.uuid4(), day=datetime.now(timezone.utc), primary_entities=[],
              status=Story.Status.QUEUED),
    ])
    await db_session.commit()

    report = await audit_table(db_session, "stories")
    assert report.table == "stories"
    assert report.total == 3
    assert report.current == 2
    assert report.stale == 1
    assert report.expected == expected_versions("stories")
    assert report.versions == {STORY_VERSION: 2, "<null>": 1}


def test_delete_order_puts_children_before_parents():
    order = delete_order(downstream_tables("dedupe"))
    for parent, child in [
        ("stories", "story_unit_links"),
        ("stories", "story_embeddings"),
        ("stories", "story_topic_groups"),
        ("stories", "claims"),
        ("canonical_entities", "entity_aliases"),
        ("claims", "claim_evidence"),
        ("reporting_units", "story_unit_links"),
    ]:
        assert order.index(child) < order.index(parent), f"{child} must go before {parent}"
    # ...and the stage's own table last, because everything referencing it is already gone.
    assert order.index("reporting_units") > order.index("story_unit_links")
    assert set(order) == set(downstream_tables("dedupe"))


def test_delete_order_says_so_instead_of_guessing_on_a_cycle():
    with pytest.raises(ValueError, match="cycle"):
        delete_order(["events", "event_geometries"])


def test_only_nothing_that_cascades_counts_as_a_blocker():
    """media_assets, snippets and event_geometries cascade; curated_posts does not."""
    pairs = reference_pairs(downstream_tables("stories"))
    assert pairs == [("curated_posts", "stories", "story_id", "id")]
    assert reference_pairs(downstream_tables("reliability")) == []
    with pytest.raises(KeyError):
        reference_pairs(["not_a_table"])


# =============================================================================
# 1b. The migration itself, against a scratch Postgres schema.
# =============================================================================

MIGRATION = "20261002050000_recomputable_derived_state.sql"
SCHEMA = "recomputable_scratch"

# gate_decisions is a ledger appended by gate runs, never a recompute stage's output, so it is not
# in STAGE_TABLES -- but it does carry both columns, declared by its own CREATE TABLE migration
# (20261002110000_gate_decisions.sql). These tests cover the ALTER-style migration, which is why
# they enumerate the tables that migration has to touch.
ALTERED_TABLES = tuple(t for t in DERIVED_TABLES if t not in NOT_RECOMPUTED_TABLES)


def _migration_sql(schema: str) -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "supabase" / "migrations" / MIGRATION
    body = path.read_text().split("DO $$", 1)[1]
    return "DO $$" + body.replace("public.", f"{schema}.")


@pytest.fixture
async def scratch():
    import os

    import asyncpg

    dsn = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        pytest.skip("no DATABASE_URL: the migration is Postgres DDL")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # cause varies: no server, refused, bad password
        pytest.skip(f"cannot reach Postgres: {exc}")
    await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {SCHEMA}")
    try:
        yield conn
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()


def test_the_migration_names_every_derived_table():
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parent.parent / "supabase" / "migrations" / MIGRATION
    ).read_text()
    missing = [t for t in ALTERED_TABLES if f"'{t}'" not in sql]
    assert not missing, f"migration never mentions {missing}"
    assert "raw_articles" not in sql.split("FOREACH", 1)[1].split("LOOP", 1)[0]


async def test_migration_adds_both_columns_and_is_idempotent(scratch):
    for table in ALTERED_TABLES:
        await scratch.execute(f"CREATE TABLE {SCHEMA}.{table} (id serial PRIMARY KEY)")
    await scratch.execute(_migration_sql(SCHEMA))

    async def columns(table):
        return {
            r["column_name"]: r["is_nullable"]
            for r in await scratch.fetch(
                "SELECT column_name, is_nullable FROM information_schema.columns"
                " WHERE table_schema = $1 AND table_name = $2",
                SCHEMA,
                table,
            )
        }

    for table in ALTERED_TABLES:
        cols = await columns(table)
        assert "analyzer_version" in cols, table
        assert "input_hash" in cols, table
        assert cols["analyzer_version"] == "YES", table
        assert cols["input_hash"] == "YES", table

    # Second apply is a no-op rather than an error, and keeps the data.
    await scratch.execute(f"INSERT INTO {SCHEMA}.stories DEFAULT VALUES")
    await scratch.execute(_migration_sql(SCHEMA))
    assert await scratch.fetchval(f"SELECT count(*) FROM {SCHEMA}.stories") == 1


async def test_migration_skips_a_table_that_is_not_there(scratch):
    await scratch.execute(f"CREATE TABLE {SCHEMA}.stories (id serial PRIMARY KEY)")
    await scratch.execute(_migration_sql(SCHEMA))
    cols = {
        r["column_name"]
        for r in await scratch.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = $1 AND table_name = 'stories'",
            SCHEMA,
        )
    }
    assert {"analyzer_version", "input_hash"} <= cols
    assert not await scratch.fetchval(
        "SELECT to_regclass($1)", f"{SCHEMA}.claims"
    )