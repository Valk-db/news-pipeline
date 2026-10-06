"""Canonical event identity: one pin per occurrence, and the rows behind it kept.

events has always been one row per story per place, so a protest carried by four papers drew
four pins and the corroboration filter read each of them as a single-sourced event. These
tests pin down the four conditions that decide whether two rows are the same occurrence
(same type, overlapping time windows, overlapping footprints, a shared entity), the
guarantees the clustering has to keep (order independence, idempotence, nothing deleted),
and the read side that turns the collapse into evidence rather than just fewer pins.

The thresholds in src/verification/event_identity.py are measured, not chosen: on the dev
corpus every true duplicate pair sits at 0.0 km and the nearest same-type pair that is not a
duplicate sits at 194.6 km, which `test_the_ceiling_sits_below_the_nearest_real_pair` records
so the constants cannot be loosened without that test being updated on purpose.

The migration tests need a real Postgres (CI sets DATABASE_URL) and skip without one, since
the migration is Postgres DDL. Everything else runs on in-memory SQLite, which is enough
because the pointer, the predicate and the aggregate are all ordinary SQL.
"""

import os
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import func, select

from curation_ui.events import _event_conditions, _event_feature
from src.schema.models import Event, Story
from src.verification.event_identity import (
    CANONICAL_EVENT_ID,
    EVENT_MATCH_RADIUS_KM,
    EVENT_TIME_WINDOW_HOURS,
    IS_CANONICAL_EVENT,
    assign_canonical_events,
    cluster_corroboration,
    cluster_events,
    cluster_tier1_sources,
    distance_km,
    event_entity_keys,
    event_radius_km,
    same_event,
    windows_overlap,
)

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "supabase"
    / "migrations"
    / "20261002120000_event_canonical_identity.sql"
)

SCHEMA = "w3eventid_scratch"

# Where three outlets reporting one protest actually sit: Tel Aviv, minutes apart.
TEL_AVIV = (32.0853, 34.7818)
WHEN = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _event(
    lat=TEL_AVIV[0],
    lon=TEL_AVIV[1],
    *,
    event_type=Event.EventType.PROTEST,
    start=WHEN,
    location_name="Tel Aviv",
    radius_km=None,
    story_id=None,
    source_count=1,
    tier1_source_count=0,
    confidence=0.5,
):
    return Event(
        id=uuid.uuid4(),
        story_id=story_id or uuid.uuid4(),
        latitude=lat,
        longitude=lon,
        location_name=location_name,
        radius_km=radius_km,
        start_time=start,
        event_type=event_type,
        confidence=confidence,
        source_count=source_count,
        tier1_source_count=tier1_source_count,
    )


def _clusters(events):
    return cluster_events(events)


# --------------------------------------------------------------------------
# The four conditions
# --------------------------------------------------------------------------


def test_three_reports_of_one_quake_become_one_canonical_event():
    """The case the brief names: one quake, three stories, one pin."""
    quake = Event.EventType.DISASTER
    reports = [
        _event(start=WHEN, event_type=quake, source_count=2, tier1_source_count=1),
        _event(start=WHEN + timedelta(minutes=40), event_type=quake),
        _event(start=WHEN + timedelta(hours=2), event_type=quake),
    ]

    clusters = _clusters(reports)

    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_a_protest_and_an_earthquake_at_one_plaza_stay_two_events():
    """Different type is a hard stop, whatever else the two rows agree on."""
    clusters = _clusters(
        [
            _event(start=WHEN, event_type=Event.EventType.PROTEST),
            _event(start=WHEN, event_type=Event.EventType.DISASTER),
            _event(start=WHEN, event_type=Event.EventType.OTHER),
        ]
    )

    assert len(clusters) == 3, "a place a story merely names is not a classified event"


def test_windows_that_touch_do_not_overlap():
    """Half-open windows: an event at 23:00 and one 24h later are two events."""
    late = WHEN + timedelta(hours=11)  # 23:00
    just_after = late + timedelta(hours=EVENT_TIME_WINDOW_HOURS)
    window = timedelta(hours=EVENT_TIME_WINDOW_HOURS)

    assert not windows_overlap((late, late + window), (just_after, just_after + window))
    assert len(_clusters([_event(start=late), _event(start=just_after)])) == 2


def test_events_just_inside_one_window_still_merge():
    """One minute inside the window is inside it."""
    first = WHEN
    second = WHEN + timedelta(hours=EVENT_TIME_WINDOW_HOURS, minutes=-1)

    assert len(_clusters([_event(start=first), _event(start=second)])) == 1


def test_distance_matches_a_known_great_circle():
    """The haversine is the whole of the location test, so check it against a known leg."""
    london = (51.5074, -0.1278)
    new_york = (40.7128, -74.0060)

    assert distance_km(*london, *new_york) == pytest.approx(5570, abs=25)


def test_the_ceiling_sits_below_the_nearest_real_pair():
    """The margin the dev corpus actually shows: 0 km duplicates, 194.6 km nearest miss."""
    assert EVENT_MATCH_RADIUS_KM * 2 == 50
    assert EVENT_MATCH_RADIUS_KM * 2 < 194.6, (
        "measured on dev: the nearest same-type pair that is not a duplicate is 194.6 km, "
        "so the match ceiling may not be raised without re-measuring"
    )


def test_footprints_must_overlap_and_may_only_be_narrowed():
    """A row's own radius narrows its reach; it can never widen it past the ceiling."""
    far = 40.0 / 111.0  # ~40 km north, inside the 50 km ceiling and outside a 5 km one
    near_enough = _event(lat=TEL_AVIV[0] + far)
    narrowed = _event(lat=TEL_AVIV[0] + far, radius_km=5.0)
    greedy = _event(lat=TEL_AVIV[0] + far, radius_km=900.0)

    assert same_event(_event(), near_enough)
    assert not same_event(_event(), narrowed)
    assert same_event(_event(), greedy), "a mis-set radius must not swallow the world"
    assert event_radius_km(greedy) == EVENT_MATCH_RADIUS_KM
    assert event_radius_km(narrowed) == 5.0
    assert event_radius_km(_event(radius_km=0.0)) == EVENT_MATCH_RADIUS_KM


def test_events_with_no_shared_entity_never_merge():
    """No key, no merge: the safe direction leaves a pin alone rather than fusing two."""
    clusters = _clusters(
        [
            _event(location_name="Tel Aviv"),
            _event(location_name="Ramat Gan"),
        ]
    )

    assert len(clusters) == 2


def test_an_event_with_no_key_at_all_never_merges():
    lonely = _event(location_name=None)
    other = _event(location_name=None)

    assert event_entity_keys(lonely) == frozenset()
    assert not same_event(lonely, other)


def test_entity_keys_survive_the_place_name_forms_the_corpus_actually_has():
    """Nominatim's 'London, London, City Of, United Kingdom' is the same key as 'London'."""
    assert event_entity_keys(_event(location_name="London, London, City Of, United Kingdom")) == event_entity_keys(
        _event(location_name="London")
    )


def test_id_keys_and_place_keys_never_collide():
    """The two key namespaces are disjoint, so a merge always means a real agreement."""
    story_ids = [str(uuid.uuid4())]

    assert event_entity_keys(_event(), story_ids).isdisjoint(event_entity_keys(_event()))


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------


def _exhaustive_clusters(events):
    """Every pair, no blocking: the reference the blocked pass has to agree with."""

    parent = {event.id: event.id for event in events}

    def find(event_id):
        while parent[event_id] != event_id:
            parent[event_id] = parent[parent[event_id]]
            event_id = parent[event_id]
        return event_id

    events = list(events)
    for first in events:
        for second in events:
            if first.id < second.id and same_event(first, second):
                parent[find(second.id)] = find(first.id)

    groups = {}
    for event in events:
        groups.setdefault(find(event.id), []).append(event)
    return sorted(
        [sorted(str(e.id) for e in group) for group in groups.values()],
    )


def _blocked_clusters(events):
    return sorted(
        sorted(str(member.id) for member in cluster.members) for cluster in _clusters(events)
    )


def test_blocks_change_the_cost_and_not_the_answer():
    """Candidate generation is an optimization, so it must not change one cluster.

    The set is built to catch the ways blocking can go wrong: duplicates that straddle a
    24 h bucket boundary, a duplicate pair at 69 degrees north where a longitude cell is
    only 39 km wide, and a same-place pair two days apart.
    """
    base = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)
    events = []
    for index in range(24):
        start = base + timedelta(hours=3 * index)
        events.append(_event(start=start, tier1_source_count=index % 3))
    for index in range(8):  # straddles the bucket edge at 2026-10-02T00:00
        events.append(_event(start=base + timedelta(hours=3 * index, minutes=45)))
    for index in range(6):  # high latitude, where a degree of longitude is a short distance
        events.append(
            _event(lat=69.6, lon=18.9 + 0.2 * index, start=base + timedelta(hours=index))
        )
    for index in range(6):  # far away, must never join anything
        events.append(_event(lat=10.0 - index, lon=100.0, start=base + timedelta(hours=index)))
    for index in range(4):  # same place, days apart
        events.append(_event(start=base + timedelta(days=index)))

    assert _blocked_clusters(events) == _exhaustive_clusters(events)
    assert len(events) == 48


def test_the_representative_is_the_best_sourced_row():
    """The row that represents a cluster is the one with the most tier 1 behind it."""
    thin = _event(tier1_source_count=0, source_count=9, confidence=0.99)
    tier1 = _event(tier1_source_count=1, source_count=1)
    busiest = _event(tier1_source_count=3, source_count=3)

    (cluster,) = _clusters([thin, tier1, busiest])

    assert cluster.canonical is busiest
    assert set(cluster.members) == {thin, tier1, busiest}


def test_the_representative_does_not_depend_on_row_order():
    """Same cluster, same representative, whatever order the rows came back in."""
    events = [
        _event(tier1_source_count=1),
        _event(tier1_source_count=1, confidence=0.9, start=WHEN + timedelta(hours=2)),
        _event(tier1_source_count=1, confidence=0.9, start=WHEN + timedelta(hours=1)),
    ]

    canonical = _clusters(events)[0].canonical.id
    for rotation in (1, 2):
        rotated = events[rotation:] + events[:rotation]
        assert _clusters(rotated)[0].canonical.id == canonical


def test_a_tie_breaks_on_the_earliest_event_then_the_lowest_id():
    """A total order, so the choice is reproducible and not left to the database."""
    later = _event(start=WHEN + timedelta(hours=3), tier1_source_count=1)
    earlier = _event(start=WHEN, tier1_source_count=1)
    twin = _event(start=WHEN, tier1_source_count=1)

    (cluster,) = _clusters([later, twin, earlier])

    assert cluster.canonical in (earlier, twin), "the later event never wins a tie"
    assert cluster.canonical.id == min(earlier.id, twin.id)


# --------------------------------------------------------------------------
# The pass, against a database
# --------------------------------------------------------------------------


def _story(db_session, primary_entities, day=datetime(2026, 10, 1, tzinfo=UTC)):
    story = Story(
        id=uuid.uuid4(),
        day=day,
        primary_entities=primary_entities,
        status=Story.Status.QUEUED,
        tier1_unit_count=1,
        distinct_owners=1,
    )
    db_session.add(story)
    return story


def _persist(db_session, stories, event_type=Event.EventType.PROTEST, hours=0, **kwargs):
    rows = [
        _event(story_id=story.id, event_type=event_type, start=WHEN + timedelta(hours=hours + i), **kwargs)
        for i, story in enumerate(stories)
    ]
    db_session.add_all(rows)
    return rows


async def test_the_pass_points_every_row_at_its_event_and_deletes_nothing(db_session):
    shared = str(uuid.uuid4())
    stories = [
        _story(db_session, [shared]),
        _story(db_session, [shared]),
        _story(db_session, [shared]),
        _story(db_session, [str(uuid.uuid4())]),
    ]
    _persist(db_session, stories)
    await db_session.commit()

    report = await assign_canonical_events(db_session)
    await db_session.commit()

    assert report.inspected == 4
    assert report.clusters == 2
    assert report.collapsed == 2

    stored = list((await db_session.execute(select(Event))).scalars().all())
    assert len(stored) == 4, "the collapse is a pointer, never a delete"
    assert all(event.canonical_event_id is not None for event in stored)
    canonical = [event for event in stored if event.canonical_event_id == event.id]
    assert len(canonical) == 2, "a canonical row points at itself"
    assert all(
        event.tier1_source_count == 0 and event.source_count == 1 for event in stored
    ), "each row keeps the reporting its own story gave it"


async def test_the_pass_is_idempotent(db_session):
    """Running it twice is the same as running it once, which is why it is safe in a pipeline."""
    shared = str(uuid.uuid4())
    stories = [_story(db_session, [shared]), _story(db_session, [shared])]
    _persist(db_session, stories)
    await db_session.commit()

    first = await assign_canonical_events(db_session)
    await db_session.commit()
    before = {
        str(event.id): str(event.canonical_event_id)
        for event in (await db_session.execute(select(Event))).scalars().all()
    }

    second = await assign_canonical_events(db_session)
    await db_session.commit()
    after = {
        str(event.id): str(event.canonical_event_id)
        for event in (await db_session.execute(select(Event))).scalars().all()
    }

    assert first == second
    assert before == after


async def test_a_shared_canonical_entity_merges_rows_with_different_place_names(db_session):
    """The pipeline anchors a story on entity ids, not on a geocoded label.

    Same coordinates, same instant, same type, and two different place names -- so the only
    thing the two rows agree on is the canonical entity their stories share, and that is
    what has to be doing the work.
    """
    shared = str(uuid.uuid4())
    stories = [
        _story(db_session, [shared]),
        _story(db_session, [shared]),
    ]
    rows = [
        _event(story_id=stories[0].id, location_name="Tel Aviv"),
        _event(story_id=stories[1].id, location_name="Ramat Gan"),
    ]
    db_session.add_all(rows)
    await db_session.commit()

    report = await assign_canonical_events(db_session)

    assert not same_event(rows[0], rows[1]), "the place names alone would not merge these"
    assert (report.clusters, report.collapsed) == (1, 1)


async def test_disjoint_entity_ids_do_not_merge_even_at_the_same_place(db_session):
    shared = str(uuid.uuid4())
    stories = [_story(db_session, [shared]), _story(db_session, [str(uuid.uuid4())])]
    _persist(db_session, stories, location_name="Tel Aviv")
    await db_session.commit()

    report = await assign_canonical_events(db_session)

    assert (report.clusters, report.collapsed) == (2, 0)


async def test_a_provenance_bag_story_keys_on_its_place_name(db_session):
    """The GDELT worker writes a dict into primary_entities and no entity ids at all.

    Its events still have to collapse, or the map keeps one pin per GKG record -- which is
    the case this whole change exists for.
    """
    bag = {"ingestion": "gdelt-daily", "location": "France", "lang": "English"}
    stories = [_story(db_session, bag), _story(db_session, bag)]
    _persist(db_session, stories, location_name="France")
    await db_session.commit()

    report = await assign_canonical_events(db_session)

    assert (report.clusters, report.collapsed) == (1, 1)


async def test_a_row_with_no_pointer_is_still_its_own_canonical_event(db_session):
    """NULL means canonical, so the table is right before any pass has ever run."""
    story = _story(db_session, [str(uuid.uuid4())])
    unpassed = _event(story_id=story.id)
    db_session.add(unpassed)
    await db_session.commit()

    visible = list(
        (
            await db_session.execute(
                select(Event).where(IS_CANONICAL_EVENT)
            )
        )
        .scalars()
        .all()
    )

    assert visible == [unpassed]


async def test_corroboration_is_counted_across_the_whole_cluster(db_session):
    """The collapse is only worth anything if the pin inherits the reporting.

    Three rows of one event with one tier 1 source each is an event three outlets carried,
    so it passes the default threshold of 2 tier 1 sources. Read per row it would have been
    filtered out, which is the bug.
    """
    shared = str(uuid.uuid4())
    stories = [_story(db_session, [shared]) for _ in range(3)]
    _persist(db_session, stories, tier1_source_count=1, source_count=2, confidence=0.3)
    await db_session.commit()
    report = await assign_canonical_events(db_session)
    await db_session.commit()

    events = list(
        (
            await db_session.execute(
                select(Event).where(IS_CANONICAL_EVENT, cluster_tier1_sources() >= 2)
            )
        )
        .scalars()
        .all()
    )

    assert (report.clusters, report.collapsed) == (1, 2)
    assert len(events) == 1, "one pin, and it passes the filter its cluster earned"

    totals = await cluster_corroboration(db_session, events)
    properties = _event_feature(events[0], totals[events[0].id])["properties"]
    assert (properties["source_count"], properties["tier1_source_count"]) == (6, 3)
    assert properties["confidence"] == 0.3, "the representative's own confidence, not a blend"


async def test_the_shared_conditions_read_canonical_events_and_cluster_counts(db_session):
    """The predicate both map endpoints use is the one the pass writes."""
    conditions = _event_conditions(min_tier1_sources=2)
    rendered = " ".join(str(condition) for condition in conditions)

    assert "canonical_event_id IS NULL" in rendered
    assert "canonical_event_id = events.id" in rendered
    assert "sum(event_cluster.tier1_source_count)" in rendered


async def test_the_story_list_counts_one_event_per_pin(db_session):
    """Two rows of one cluster are one event in the story list, as they are on the map."""
    shared = str(uuid.uuid4())
    story = _story(db_session, [shared])
    _persist(db_session, [story], hours=0)
    extra = _event(story_id=story.id, start=WHEN + timedelta(hours=1))
    db_session.add(extra)
    await db_session.commit()
    await assign_canonical_events(db_session)
    await db_session.commit()

    counts = (
        await db_session.execute(
            select(Event.story_id, func.count(func.distinct(CANONICAL_EVENT_ID)))
            .group_by(Event.story_id)
        )
    ).all()

    assert (await db_session.execute(select(func.count(Event.id)))).scalar() == 2, (
        "two rows really were written"
    )
    assert counts == [(story.id, 1)], "and the list reports the one pin they make"


# --------------------------------------------------------------------------
# The migration, against a real Postgres scratch schema
# --------------------------------------------------------------------------


def migration_sql(schema: str) -> str:
    """The migration with its `public.` qualifier aimed at a scratch schema."""
    return MIGRATION.read_text().replace("public.", f"{schema}.")


@pytest.fixture
async def scratch():
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


async def create_events_table(conn: asyncpg.Connection) -> None:
    """A scratch events in its pre-migration shape: an id, and no canonical pointer.

    The column has to be absent, or ADD COLUMN IF NOT EXISTS skips it and the migration
    silently tests nothing.
    """
    await conn.execute(
        f"CREATE TABLE {SCHEMA}.events (id uuid PRIMARY KEY, location_name text)"
    )


async def foreign_key_def(conn) -> str:
    return await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass($1) AND contype = 'f'",
        f"{SCHEMA}.events",
    )


async def test_migration_adds_a_nullable_self_reference(scratch):
    await create_events_table(scratch)

    await scratch.execute(migration_sql(SCHEMA))

    assert "ON DELETE SET NULL" in await foreign_key_def(scratch)
    nullable = await scratch.fetchval(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = 'events' AND column_name = 'canonical_event_id'",
        SCHEMA,
    )
    assert nullable == "YES"
    assert await scratch.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE schemaname = $1 AND indexname = $2",
        SCHEMA,
        "ix_events_canonical_event_id",
    ) == 1


async def test_applying_the_migration_twice_changes_nothing(scratch):
    """Re-apply is a no-op: no error, and the pointers written in between survive."""
    await create_events_table(scratch)
    canonical, duplicate = uuid.uuid4(), uuid.uuid4()
    await scratch.execute(migration_sql(SCHEMA))
    await scratch.execute(
        f"INSERT INTO {SCHEMA}.events (id, location_name, canonical_event_id) "
        "VALUES ($1, 'Tel Aviv', $1), ($2, 'Tel Aviv', $1)",
        canonical,
        duplicate,
    )

    await scratch.execute(migration_sql(SCHEMA))

    assert await scratch.fetchval(f"SELECT count(*) FROM {SCHEMA}.events") == 2
    assert await scratch.fetchval(
        f"SELECT canonical_event_id FROM {SCHEMA}.events WHERE id = $1", duplicate
    ) == canonical


async def test_deleting_a_canonical_event_never_deletes_the_rows_that_pointed_at_it(scratch):
    """ON DELETE SET NULL, not CASCADE: a collapsed row outlives the row it pointed at.

    Deleting the representative must not take the evidence with it. The rows read back as
    their own canonical events, which is the same reading a row that never had a pointer has.
    """
    await create_events_table(scratch)
    await scratch.execute(migration_sql(SCHEMA))
    canonical, duplicate = uuid.uuid4(), uuid.uuid4()
    await scratch.execute(
        f"INSERT INTO {SCHEMA}.events (id, location_name, canonical_event_id) "
        "VALUES ($1, 'Tel Aviv', $1), ($2, 'Tel Aviv', $1)",
        canonical,
        duplicate,
    )

    await scratch.execute(f"DELETE FROM {SCHEMA}.events WHERE id = $1", canonical)

    assert await scratch.fetchval(f"SELECT count(*) FROM {SCHEMA}.events") == 1
    assert await scratch.fetchval(
        f"SELECT canonical_event_id FROM {SCHEMA}.events WHERE id = $1", duplicate
    ) is None
