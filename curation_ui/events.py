"""The event query and GeoJSON serialization shared by /api/globe/events and
/api/map/replay.

Both endpoints read the same Event table with the same filters and must emit the
same feature shape, so the WHERE-clause builder and the Feature serializer live
here rather than being written twice.

Both also read *canonical* events. One occurrence is one pin: rows that a producer wrote for
the same quake or the same protest collapse onto a single representative row through
`events.canonical_event_id`, and this is the one place that decides so. A row with no
pointer is its own canonical event, so the table is correct before any pass has run, and the
corroboration filter reads the *cluster's* sources rather than one row's -- otherwise
collapsing four rows of one event would leave the pin looking single-sourced and the filter
would hide the best-corroborated events on the map. See src/verification/event_identity.py.

Both endpoints are anonymous, and this builder is also where the public-status rule lives.
An event is reachable from its story by a single NOT NULL foreign key (`events.story_id`), so
the rule is a semi-join against public story ids rather than a JOIN the callers have to
remember to add: `_apply_story_tier_filter` already joins `stories` for its own reason, a
second bare join would alias, and a caller that forgot the join would silently serve the
whole table. It belongs here, in the one builder both endpoints call, because
/api/globe/events and /api/map/replay drifted away from PUBLIC_STORY_STATUSES exactly once
already: the rule had been written into the endpoints instead of the shared clause, so the
map served pins for BLOCKED and PENDING stories whose own pages 404'd.
"""

import uuid
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import Float

from curation_ui.discovery import PUBLIC_STORY_STATUSES
from src.schema.models import Event, Story
from src.verification.event_identity import (
    IS_CANONICAL_EVENT,
    cluster_tier1_sources,
)


def public_story_condition() -> ColumnElement:
    """The event predicate that enforces PUBLIC_STORY_STATUSES.

    Every events row carries a non-null story_id foreign key into stories, so no event
    can belong to a non-public story and pass this predicate. That is also how an
    event with no story at all would be treated if the schema ever allowed one:
    dropped, because IN over a subquery cannot match a missing parent. Failing closed
    is the only safe direction for a public surface -- a pin nobody can trace to a
    published story is the exact defect this rule exists to prevent, and there is no
    legitimate orphaned case to preserve.
    """
    return Event.story_id.in_(
        select(Story.id).where(Story.status.in_(PUBLIC_STORY_STATUSES))
    )


def _event_conditions(
    layer_id: uuid.UUID = None,
    event_type: str = None,
    min_confidence: float = None,
    bbox: str = None,
    start: datetime = None,
    end: datetime = None,
    min_tier1_sources: int = None,
) -> list:
    """Build the WHERE clauses shared by the event GeoJSON endpoints.

    The public-status rule is unconditional here and cannot be switched off by a
    caller: every returned list contains it, so an endpoint cannot serve a pin for a
    story that is still in the triage queue, rejected, or blocked.
    An unparseable bbox is ignored (matches the historical behavior);
    start/end arrive already parsed by _parse_iso_timestamp.
    min_tier1_sources applies the tier 1 corroboration threshold, counted across
    the whole canonical event rather than across one story's row of it.
    """
    conditions = [IS_CANONICAL_EVENT, public_story_condition()]
    if layer_id:
        conditions.append(Event.layer_id == layer_id)
    if event_type:
        conditions.append(Event.event_type == event_type)
    if min_confidence:
        conditions.append(Event.confidence.cast(Float) >= min_confidence)
    if min_tier1_sources:
        conditions.append(cluster_tier1_sources() >= min_tier1_sources)
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, bbox.split(","))
            conditions.append(
                and_(
                    Event.longitude.cast(Float) >= min_lon,
                    Event.longitude.cast(Float) <= max_lon,
                    Event.latitude.cast(Float) >= min_lat,
                    Event.latitude.cast(Float) <= max_lat,
                )
            )
        except ValueError:
            pass  # Invalid bbox, ignore
    if start is not None:
        conditions.append(Event.start_time >= start)
    if end is not None:
        conditions.append(Event.start_time <= end)
    return conditions


def _event_feature(event: Event, corroboration: tuple = None) -> dict:
    """Serialize one Event as a GeoJSON Feature.

    Shared by /api/globe/events and /api/map/replay so both endpoints emit
    identical properties. Falls back to the event's own lat/lon when no
    EventGeometry row is attached.

    `corroboration` is the (source_count, tier1_source_count) the event's whole
    cluster reported, from `cluster_corroboration`; without it the row's own counts are
    used, which is correct for a row that is its own event and understates one that is not.
    """
    source_count, tier1_source_count = corroboration or (
        event.source_count,
        event.tier1_source_count,
    )
    properties = {
        "story_id": str(event.story_id),
        "layer_id": str(event.layer_id) if event.layer_id else None,
        "event_type": event.event_type.value if event.event_type else None,
        "confidence": float(event.confidence) if event.confidence else 0.5,
        "source_count": source_count,
        "tier1_source_count": tier1_source_count,
        "location_name": event.location_name,
        "location_type": event.location_type,
        "radius_km": float(event.radius_km) if event.radius_km else None,
        "start_time": event.start_time.isoformat() if event.start_time else None,
        "entities": event.entities,
    }

    geometry = event.geometry
    if geometry and geometry.geojson:
        return {
            "type": "Feature",
            "id": str(event.id),
            "geometry": geometry.geojson,
            "properties": {**properties, **(geometry.properties or {})},
        }

    return {
        "type": "Feature",
        "id": str(event.id),
        "geometry": {
            "type": "Point",
            "coordinates": [float(event.longitude), float(event.latitude)],
        },
        "properties": properties,
    }