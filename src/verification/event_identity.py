"""Canonical event identity: which map pins are the same occurrence.

`events` has always been one row per story per place. `scripts/backfill_globe_events.py`
places a story at the most specific located entity it names; the GDELT worker writes one row
per GKG record it geocodes. Nothing ever asked whether two of those rows were the same
occurrence, so a protest carried by four papers drew four pins on one plaza, and
`/api/globe/stats` counted the news four times. The corroboration filter then read each of
those pins as a single-sourced event, which is the exact opposite of what four papers
carrying one event means.

`canonical_event_id` makes that judgement a row instead of a guess. Two events are the same
occurrence when **all four** of these hold:

1. **Same type.** Strict equality, so a protest and an earthquake at one plaza stay two
   events and `OTHER` (a geocoded place a story merely *names*, which is all the pipeline's
   rows assert) never merges with a classified occurrence. Equality also makes the relation a
   partition, so union-find cannot chain a cluster across types.
2. **Overlapping time windows.** Half-open `[start_time, start_time + 24h)`, so two windows
   that merely touch -- an event at 23:00 and a different one at 23:00 the next day -- do
   not overlap. 24h is the only duration news itself distinguishes: every measured duplicate
   pair sits inside one day and the nearest non-duplicate straddles one.
3. **Overlapping footprints.** `distance_km(a, b) <= radius_a + radius_b`, haversine on the
   great circle. Measured on the dev corpus, every true duplicate pair is at **0.0 km** and
   the nearest same-type pair that is *not* a duplicate is at **194.6 km**, so the 25 km
   ceiling sits 7.8x below the first non-duplicate and the real gap in the data is a factor
   of two hundred. A row may narrow its own reach (a small `radius_km` means "this is a
   point, not a place") but never widen it past the ceiling, so one mis-set radius cannot
   merge the world.
4. **A shared entity.** A canonical entity id from the story, or -- where the producer
   records no entity ids -- the place name the row was written for. The two key namespaces
   are disjoint (entity ids are raw uuids, place keys are `_normalize_text`-shaped and
   prefixed `GPE:`), so an id can never collide with a place. An event with no key at all
   never merges with anything, which is the safe direction.

Clustering is union-find over that predicate, so the relation is transitive by construction
and the answer does not depend on the order rows come back from the database. The
representative is chosen by a total order on the row's own evidence --
`(tier1_source_count, source_count, confidence) desc, then (start_time, id) asc` -- so a
re-run picks the same row, and the representative is always a row that is really in the
cluster.

Candidate generation blocks on `(event_type, floor(start_time / 24h), floor(latitude))` and
compares each block with its eight neighbours. That is complete, not approximate: two events
close enough to match are less than 50 km and less than 24 h apart, so they differ by at most
one cell on each of the two blocked coordinates. A degree grid on longitude would *not* have
that property -- a cell narrows to 38 km across at 70 degrees north, so a 3x3 neighbourhood
is not provably sufficient there -- which is why the grid is built from the time window and
the meridian, the two axes whose cell size does not shrink with latitude.

Nothing is deleted and nothing is recomputed here. The pass writes pointers and stops: a
canonical row points at itself, a collapsed row points at the row that represents its cluster,
and each row's own `source_count`/`tier1_source_count` keeps meaning "what this story's
reporting said". The corroboration of a *cluster* is a read-time aggregate
(`cluster_source_count`, `cluster_tier1_sources`), because a stored sum on the row being
summed cannot be recomputed twice without double-counting, and this pass has to be safe to
re-run. `analyzer_version` is deliberately untouched: it names the analyzer that created the
row, and this pass created nothing. Its own record of what it did is the pointer.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from src.schema.models import Event, Story


# NOTE: src.utils.ner is intentionally NOT imported at module level.
# src/utils/ is excluded from the Vercel serverless bundle (spacy), so a
# top-level import here would crash the UI deploy. canonical_surface is
# imported lazily inside _event_keys() instead.

# Measured on the dev corpus, not guessed: duplicates at 0.0 km, nearest non-duplicate
# 194.6 km. See the module docstring.
EVENT_MATCH_RADIUS_KM = 25.0

# The only window news itself distinguishes. Half-open, so touching windows do not overlap.
EVENT_TIME_WINDOW_HOURS = 24

# Candidate-generation cell on the meridian (~111 km). Only has to be wider than twice the
# match radius for the 3x3 neighbourhood to be provably complete.
EVENT_LOCATION_CELL_DEG = 1.0

EARTH_RADIUS_KM = 6371.0088

# A row that carries no pointer is its own canonical event. This is the backward-compatible
# reading: producers outside this repo write events over REST and never set the column, so
# every pre-existing row is NULL and every pre-existing row is correct without a backfill.
IS_CANONICAL_EVENT = or_(
    Event.canonical_event_id.is_(None),
    Event.canonical_event_id == Event.id,
)

# The key a row clusters under, as SQL. NULL collapses onto the row's own id, so grouping and
# filtering agree on the same answer whether or not the pass has run yet.
CANONICAL_EVENT_ID = func.coalesce(Event.canonical_event_id, Event.id)

# The same table again, aliased, for the per-row aggregate below. An alias rather than
# correlation: a correlated subquery over the enclosing `events` would remove its only FROM
# and produce invalid SQL, and the ambiguity of an unaliased self-reference is not worth the
# risk of getting wrong silently.
_CLUSTER = Event.__table__.alias("event_cluster")


def _cluster_id(alias=_CLUSTER) -> ColumnElement:
    return func.coalesce(alias.c.canonical_event_id, alias.c.id)


def _cluster_total(column) -> ColumnElement:
    """Sum `column` over every row that clusters under the same canonical event."""
    return (
        select(func.coalesce(func.sum(column), 0))
        .select_from(_CLUSTER)
        .where(_cluster_id() == CANONICAL_EVENT_ID)
        .correlate(Event)
        .scalar_subquery()
    )


def cluster_source_count() -> ColumnElement:
    """Total sources across the cluster, as a scalar subquery over the enclosing row."""
    return _cluster_total(_CLUSTER.c.source_count)


def cluster_tier1_sources() -> ColumnElement:
    """Total tier 1 sources across the cluster, as a scalar subquery over the enclosing row."""
    return _cluster_total(_CLUSTER.c.tier1_source_count)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def event_radius_km(event: Event) -> float:
    """The distance within which `event` may be matched to another.

    A row's own `radius_km` may narrow this and never widen it past the ceiling, so a single
    bad radius cannot make an event swallow the world. Rows with no radius get the ceiling.
    """
    radius = event.radius_km
    if radius is not None and 0 < radius <= EVENT_MATCH_RADIUS_KM:
        return float(radius)
    return EVENT_MATCH_RADIUS_KM


def _utc(value: datetime) -> datetime:
    """Read a timestamp as aware UTC. SQLite has no timezone type, so a
    DateTime(timezone=True) column comes back naive there, and a naive value compared
    against an aware one raises instead of answering. Naive input is read as UTC, which is
    what the column stores.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_start(event: Event) -> datetime:
    return _utc(event.start_time)


def event_time_window(event: Event, hours: int = EVENT_TIME_WINDOW_HOURS) -> tuple[datetime, datetime]:
    """The half-open window an event is considered to occupy: [start, start + hours)."""
    start = _event_start(event)
    return start, start + timedelta(hours=hours)


def windows_overlap(first: tuple[datetime, datetime], second: tuple[datetime, datetime]) -> bool:
    """Whether two half-open windows share any instant. Touching windows do not overlap."""
    return first[0] < second[1] and second[0] < first[1]


def event_entity_keys(event: Event, story_entities=None) -> frozenset[str]:
    """The keys an event is anchored to, in a namespace nothing else writes into.

    A story's `primary_entities` is a list of canonical entity ids for the pipeline's stories
    and a provenance bag for the GDELT worker's, so the list is used when there is one and
    ignored otherwise. An event with no ids and no place name has no key and never merges
    with anything, which is the direction that leaves a pin alone rather than fusing two.
    """
    keys = (
        frozenset(str(key) for key in story_entities if key)
        if isinstance(story_entities, list)
        else frozenset()
    )
    if keys:
        return keys
    if event.location_name:
        # Lazy: src.utils/ is excluded from the serverless bundle.
        from src.utils.ner import canonical_surface

        return frozenset({canonical_surface(event.location_name, "GPE")})
    return frozenset()


def same_event(
    first: Event,
    second: Event,
    keys: Mapping | None = None,
) -> bool:
    """Whether two events are the same occurrence. All four conditions, or nothing.

    `keys` maps event id to the keys from `event_entity_keys`; without it both events fall
    back to their place name, which is the only signal the non-pipeline rows carry.
    """
    if first.id == second.id or first.event_type != second.event_type:
        return False
    if not windows_overlap(event_time_window(first), event_time_window(second)):
        return False
    if distance_km(first.latitude, first.longitude, second.latitude, second.longitude) > (
        event_radius_km(first) + event_radius_km(second)
    ):
        return False
    first_keys = keys[first.id] if keys else event_entity_keys(first)
    second_keys = keys[second.id] if keys else event_entity_keys(second)
    return bool(first_keys & second_keys)


class _UnionFind:
    """Union-find over event ids, so the cluster of a relation does not depend on order."""

    def __init__(self, ids: Iterable) -> None:
        self._parent = {event_id: event_id for event_id in ids}

    def find(self, event_id):
        root = event_id
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[event_id] != root:  # path compression
            self._parent[event_id], event_id = root, self._parent[event_id]
        return root

    def union(self, first, second) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            self._parent[second_root] = first_root


def _block(event: Event) -> tuple:
    """The candidate-generation cell: type, 24 h bucket, and 1 degree of latitude."""
    return (
        event.event_type,
        _event_start(event).timestamp() // (EVENT_TIME_WINDOW_HOURS * 3600),
        math.floor(event.latitude / EVENT_LOCATION_CELL_DEG),
    )


def _neighbour_blocks(event: Event) -> Iterable[tuple]:
    """The nine cells whose members could match `event`: itself plus one either way."""
    event_type, bucket, band = _block(event)
    for bucket_shift in (-1, 0, 1):
        for band_shift in (-1, 0, 1):
            yield (event_type, bucket + bucket_shift, band + band_shift)


@dataclass(frozen=True)
class EventCluster:
    """One occurrence, and every event row that reports it."""

    canonical: Event
    members: tuple[Event, ...]

    def __len__(self) -> int:
        return len(self.members)


def _representative(cluster: Sequence[Event]) -> Event:
    """The row that represents a cluster, by the row's own evidence.

    Most tier 1 sourcing, then most sourcing, then the most confident, and on a tie the
    earliest event, then the lowest id -- a total order, so the same cluster always resolves
    to the same row whatever order the database returned the rows in.
    """
    return min(
        cluster,
        key=lambda event: (
            -(event.tier1_source_count or 0),
            -(event.source_count or 0),
            -(event.confidence or 0.0),
            _event_start(event),
            str(event.id),
        ),
    )


def cluster_events(
    events: Sequence[Event],
    keys: Mapping | None = None,
) -> list[EventCluster]:
    """Group events into occurrences, one cluster per occurrence.

    Pairs are proposed by the 3x3 block neighbourhood and decided by `same_event`, so the
    blocks change the cost and not the answer: an exhaustive pairwise pass over the same rows
    produces the same clusters.
    """
    blocks: dict[tuple, list[Event]] = {}
    for event in events:
        blocks.setdefault(_block(event), []).append(event)

    union = _UnionFind(event.id for event in events)
    seen = set()
    for event in events:
        for block in _neighbour_blocks(event):
            for other in blocks.get(block, ()):
                pair = (str(event.id), str(other.id))
                if pair in seen:
                    continue
                seen.add(pair)
                if same_event(event, other, keys):
                    union.union(event.id, other.id)

    members: dict[object, list[Event]] = {}
    for event in events:
        members.setdefault(union.find(event.id), []).append(event)
    return [
        EventCluster(canonical=_representative(group), members=tuple(group))
        for group in members.values()
    ]


async def cluster_corroboration(session, events: Sequence[Event]) -> dict:
    """Corroboration summed over each event's cluster: {event id: (sources, tier 1)}.

    One grouped query for the whole page. This is where the collapse turns into evidence --
    four rows for one protest read as one pin carried by four outlets, which is what the
    corroboration filter is asking about -- and it is a read rather than a stored sum because
    a sum written onto the row being summed cannot be recomputed without double-counting.
    """
    ids = [event.id for event in events]
    if not ids:
        return {}
    rows = await session.execute(
        select(
            CANONICAL_EVENT_ID.label("canonical_id"),
            func.sum(Event.source_count).label("source_count"),
            func.sum(Event.tier1_source_count).label("tier1_source_count"),
        )
        .where(CANONICAL_EVENT_ID.in_(ids))
        .group_by(CANONICAL_EVENT_ID)
    )
    return {row[0]: (int(row[1] or 0), int(row[2] or 0)) for row in rows}


@dataclass(frozen=True)
class DedupReport:
    """What one pass over the events table did."""

    inspected: int
    clusters: int
    collapsed: int


async def assign_canonical_events(
    session,
    events: Sequence[Event] | None = None,
) -> DedupReport:
    """Point every event row at the event that represents it. Returns what it changed.

    Reads the whole table when given no events, so rows written by producers outside this
    repo take part in the collapse: the problem was never "the pipeline made two events", it
    was "two producers each made one event for one occurrence". The function is a pure
    function of the rows -- existing pointers are never read, and nothing but
    `canonical_event_id` is written -- so running it twice is the same as running it once.
    """
    if events is None:
        events = list((await session.execute(select(Event))).scalars().all())
    if not events:
        return DedupReport(inspected=0, clusters=0, collapsed=0)

    story_ids = {event.story_id for event in events if event.story_id}
    story_entities: dict = {}
    if story_ids:
        rows = await session.execute(
            select(Story.id, Story.primary_entities).where(Story.id.in_(story_ids))
        )
        story_entities = {row[0]: row[1] for row in rows}

    keys = {
        event.id: event_entity_keys(event, story_entities.get(event.story_id))
        for event in events
    }

    clusters = cluster_events(events, keys)
    for cluster in clusters:
        for member in cluster.members:
            member.canonical_event_id = cluster.canonical.id

    return DedupReport(
        inspected=len(events),
        clusters=len(clusters),
        collapsed=len(events) - len(clusters),
    )
