"""The public globe JSON API: the event FeatureCollection and its two companions.

These three routes are anonymous and read only. The event query itself lives in
curation_ui.events because /api/map/replay runs the identical WHERE clause and
emits the identical Feature shape; the clamps that make the endpoints safe to
serve anonymously live here next to the parameters they bound.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import selectinload

from curation_ui.app_state import check_database_public
from curation_ui.discovery import (
    MAP_DEFAULT_WINDOW_HOURS,
    MAP_EVENTS_MAX_LIMIT,
    _apply_story_tier_filter,
    _corroboration_filter,
    _parse_tiers,
    _resolve_window,
)
from curation_ui.events import _event_conditions, _event_feature
from src.schema.models import Event, EventLayer
from src.shared.database import get_session
from src.verification.event_identity import IS_CANONICAL_EVENT, cluster_corroboration

router = APIRouter()

# /api/globe/layers is anonymous, and event_layers is a table nothing in the
# request path can bound: the cap is defense in depth, the way MAP_EVENTS_MAX_LIMIT
# is for the event endpoints. The effective cap is reported back so a truncated
# layer list is visible instead of silently short.
MAP_LAYERS_MAX_LIMIT = 200


@router.get("/api/globe/events")
async def get_globe_events(
    request: Request,
    layer_id: uuid.UUID = None,
    event_type: str = None,
    min_confidence: float = None,
    bbox: str = None,  # min_lon,min_lat,max_lon,max_lat
    hours: int = MAP_DEFAULT_WINDOW_HOURS,
    min_tier1_sources: int = None,
    limit: int = 500,
    tiers: str = None,
):
    """Get events as GeoJSON FeatureCollection for globe visualization.

    Public and read only, and one pin per occurrence: rows that report the same event
    collapse onto a canonical event, and the corroboration counts on a pin are the ones
    every story in its cluster reported. The default window is the last
    MAP_DEFAULT_WINDOW_HOURS hours filtered to corroborated events (tier1_source_count
    >= 2 across the cluster, the same threshold apply_tier1_gate uses for stories); pass
    hours=0 for all time and min_tier1_sources=0 to turn the corroboration filter off.
    tiers (e.g. "1,2") keeps only events whose story has a unit in an included tier.
    limit is clamped to MAP_EVENTS_MAX_LIMIT and the effective cap is reported back.
    """
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return {"error": db_msg}

    window_start, window_end = _resolve_window(hours, datetime.now(timezone.utc))
    threshold = _corroboration_filter(min_tier1_sources)
    effective_limit = max(1, min(limit, MAP_EVENTS_MAX_LIMIT))

    async with get_session() as session:
        stmt = select(Event).options(selectinload(Event.geometry), selectinload(Event.layer))
        stmt = _apply_story_tier_filter(stmt, _parse_tiers(tiers))

        conditions = _event_conditions(
            layer_id=layer_id,
            event_type=event_type,
            min_confidence=min_confidence,
            bbox=bbox,
            start=window_start,
            end=window_end,
            min_tier1_sources=threshold or None,
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(desc(Event.start_time)).limit(effective_limit)
        result = await session.execute(stmt)
        events = result.scalars().all()
        corroboration = await cluster_corroboration(session, events)

    return {
        "type": "FeatureCollection",
        "features": [_event_feature(e, corroboration.get(e.id)) for e in events],
        "count": len(events),
        "limit": effective_limit,
        "max_limit": MAP_EVENTS_MAX_LIMIT,
    }


@router.get("/api/globe/stats")
async def get_globe_stats(
    request: Request,
):
    """Get globe statistics."""
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        # Canonical events, so these counts are the pins on the map rather than the rows
        # behind them: a protest carried by four papers is one event four times over, and a
        # stats panel that says 32 while the globe draws 26 is reporting two different
        # questions with one number.
        # Total events
        total_result = await session.execute(
            select(func.count(Event.id)).where(IS_CANONICAL_EVENT)
        )
        total_events = total_result.scalar()

        # By event type
        type_result = await session.execute(
            select(Event.event_type, func.count(Event.id))
            .where(IS_CANONICAL_EVENT)
            .group_by(Event.event_type)
        )
        by_type = {row[0].value if row[0] else "other": row[1] for row in type_result.all()}

        # By layer
        layer_result = await session.execute(
            select(EventLayer.name, func.count(Event.id))
            .outerjoin(Event, and_(Event.layer_id == EventLayer.id, IS_CANONICAL_EVENT))
            .group_by(EventLayer.name)
        )
        by_layer = {row[0] or "unlayered": row[1] for row in layer_result.all()}

        # Date range
        date_result = await session.execute(
            select(func.min(Event.start_time), func.max(Event.start_time))
        )
        min_date, max_date = date_result.first()
        date_range = [
            min_date.isoformat() if min_date else None,
            max_date.isoformat() if max_date else None,
        ]

    return {
        "total_events": total_events,
        "by_type": by_type,
        "by_layer": by_layer,
        "date_range": date_range,
    }


@router.get("/api/globe/layers")
async def get_globe_layers(
    request: Request,
):
    """Get all event layers for globe visualization."""
    db_ok, db_msg = check_database_public(request)
    if not db_ok:
        return {"error": db_msg}

    async with get_session() as session:
        stmt = select(EventLayer).order_by(EventLayer.name).limit(MAP_LAYERS_MAX_LIMIT)
        result = await session.execute(stmt)
        layers = result.scalars().all()

        return {
            "layers": [
                {
                    "id": str(layer.id),
                    "name": layer.name,
                    "description": layer.description,
                    "filter_criteria": layer.filter_criteria,
                    "style": layer.style,
                    "is_default": layer.is_default,
                    "is_visible": layer.is_visible,
                    "min_zoom": layer.min_zoom,
                    "max_zoom": layer.max_zoom,
                    "color": layer.color,
                }
                for layer in layers
            ],
            "count": len(layers),
            "limit": MAP_LAYERS_MAX_LIMIT,
            "max_limit": MAP_LAYERS_MAX_LIMIT,
        }