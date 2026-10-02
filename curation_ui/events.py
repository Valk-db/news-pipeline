"""The event query and GeoJSON serialization shared by /api/globe/events and
/api/map/replay.

Both endpoints read the same Event table with the same filters and must emit the
same feature shape, so the WHERE-clause builder and the Feature serializer live
here rather than being written twice.
"""

import uuid
from datetime import datetime

from sqlalchemy import and_
from sqlalchemy.types import Float

from src.schema.models import Event


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

    An unparseable bbox is ignored (matches the historical behavior);
    start/end arrive already parsed by _parse_iso_timestamp.
    min_tier1_sources applies the tier 1 corroboration threshold.
    """
    conditions = []
    if layer_id:
        conditions.append(Event.layer_id == layer_id)
    if event_type:
        conditions.append(Event.event_type == event_type)
    if min_confidence:
        conditions.append(Event.confidence.cast(Float) >= min_confidence)
    if min_tier1_sources:
        conditions.append(Event.tier1_source_count >= min_tier1_sources)
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


def _event_feature(event: Event) -> dict:
    """Serialize one Event as a GeoJSON Feature.

    Shared by /api/globe/events and /api/map/replay so both endpoints emit
    identical properties. Falls back to the event's own lat/lon when no
    EventGeometry row is attached.
    """
    properties = {
        "story_id": str(event.story_id),
        "layer_id": str(event.layer_id) if event.layer_id else None,
        "event_type": event.event_type.value if event.event_type else None,
        "confidence": float(event.confidence) if event.confidence else 0.5,
        "source_count": event.source_count,
        "tier1_source_count": event.tier1_source_count,
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