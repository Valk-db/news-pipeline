"""Tests for globe visualization functionality."""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.enrichment.geocoder import Geocoder, parse_nominatim_hit
from src.schema.models import (
    CanonicalEntity, Event, EventGeometry, EventLayer, Story
)

from scripts.backfill_globe_events import (
    PLACE_SPECIFICITY,
    UNKNOWN_SPECIFICITY,
    group_by_story,
    most_specific,
    story_event,
)


NOMINATIM_HIT = {
    "display_name": "New York City, New York, United States",
    "lat": "40.7128",
    "lon": "-74.0060",
    "addresstype": "city",
    "type": "administrative",
    "class": "place",
    "importance": 0.7396,
}


def test_event_model_fields():
    """Test that Event model has all required fields."""
    event = Event(
        id=uuid.uuid4(),
        story_id=uuid.uuid4(),
        latitude=40.7128,
        longitude=-74.0060,
        location_name="New York City",
        location_type="city",
        radius_km=25.0,
        start_time=datetime.now(timezone.utc),
        event_type=Event.EventType.CONFLICT,
        confidence=0.85,
        source_count=5,
        tier1_source_count=3,
        entities={"PERSON": ["John"], "ORG": ["UN"]},
    )
    assert event.latitude == 40.7128
    assert event.longitude == -74.0060
    assert event.event_type == Event.EventType.CONFLICT
    assert event.confidence == 0.85


def test_event_type_enum():
    """Test EventType enum values."""
    assert Event.EventType.CONFLICT == "conflict"
    assert Event.EventType.PROTEST == "protest"
    assert Event.EventType.ELECTION == "election"
    assert Event.EventType.DISASTER == "disaster"
    assert Event.EventType.ACCIDENT == "accident"
    assert Event.EventType.POLITICAL == "political"
    assert Event.EventType.ECONOMIC == "economic"
    assert Event.EventType.HEALTH == "health"
    assert Event.EventType.ENVIRONMENTAL == "environmental"
    assert Event.EventType.CRIME == "crime"
    assert Event.EventType.SPORTS == "sports"
    assert Event.EventType.CULTURAL == "cultural"
    assert Event.EventType.SCIENTIFIC == "scientific"
    assert Event.EventType.OTHER == "other"


def test_event_geometry_model():
    """Test EventGeometry model."""

    geom = EventGeometry(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        geometry_type=EventGeometry.GeometryType.POINT,
        geojson={"type": "Point", "coordinates": [-74.0060, 40.7128]},
        properties={"population": 8000000},
    )
    assert geom.geometry_type == EventGeometry.GeometryType.POINT
    assert geom.geojson["type"] == "Point"


def test_event_layer_model():
    """Test EventLayer model."""

    layer = EventLayer(
        id=uuid.uuid4(),
        name="Conflicts",
        description="Armed conflicts worldwide",
        filter_criteria={"event_type": ["conflict"], "min_confidence": 0.7},
        style={"color": "red", "radius": 10000},
        is_default=True,
        is_visible=True,
        min_zoom=1.0,
        max_zoom=10.0,
    )
    assert layer.name == "Conflicts"
    assert layer.filter_criteria["event_type"] == ["conflict"]
    assert layer.is_default is True


def test_event_layer_style():
    """Test EventLayer style configuration."""

    layer = EventLayer(
        id=uuid.uuid4(),
        name="Heatmap",
        filter_criteria={},
        style={
            "color": "red",
            "radius": 50000,
            "opacity": 0.7,
            "blend_mode": "additive",
        },
        is_default=False,
        is_visible=True,
    )
    assert layer.style["color"] == "red"
    assert layer.style["radius"] == 50000


def test_event_geometry_types():
    """Test EventGeometry geometry types."""

    for geom_type in ["point", "polygon", "linestring", "multipolygon", "multilinestring"]:
        geom = EventGeometry(
            id=uuid.uuid4(),
            event_id=uuid.uuid4(),
            geometry_type=EventGeometry.GeometryType(geom_type),
            geojson={"type": geom_type.capitalize(), "coordinates": []},
        )
        assert geom.geometry_type.value == geom_type


def test_canonical_entity_geolocation():
    """Test CanonicalEntity with geolocation fields."""
    entity = CanonicalEntity(
        id=uuid.uuid4(),
        canonical_name="Paris",
        entity_type="LOC",
        latitude=48.8566,
        longitude=2.3522,
        location_type="city",
        geonames_id="2988507",
        geojson={"type": "Polygon", "coordinates": [[[2.2, 48.8], [2.4, 48.8], [2.4, 48.9], [2.2, 48.9], [2.2, 48.8]]]},
    )
    assert entity.latitude == 48.8566
    assert entity.longitude == 2.3522
    assert entity.location_type == "city"
    assert entity.geonames_id == "2988507"


# --- geocoder ---------------------------------------------------------------


def test_parse_nominatim_hit():
    """A Nominatim hit becomes a point plus the granularity label."""
    result = parse_nominatim_hit(NOMINATIM_HIT)
    assert result.latitude == 40.7128
    assert result.longitude == -74.0060
    # addresstype is the useful label; `type` is the fallback.
    assert result.location_type == "city"
    assert result.importance == 0.7396


def test_parse_nominatim_hit_rejects_unusable_points():
    """A hit without coordinates, or with coordinates off the globe, is not a place."""
    assert parse_nominatim_hit({"display_name": "nowhere"}) is None
    assert parse_nominatim_hit({"lat": "abc", "lon": "1"}) is None
    assert parse_nominatim_hit({"lat": "91", "lon": "0"}) is None
    assert parse_nominatim_hit({"lat": "0", "lon": "181"}) is None


def test_parse_nominatim_hit_falls_back_to_type():
    """A hit without addresstype still carries a granularity label."""
    assert parse_nominatim_hit({"lat": "0", "lon": "0", "type": "country"}).location_type == "country"
    assert parse_nominatim_hit({"lat": "0", "lon": "0"}).location_type == "place"


def test_parse_nominatim_hit_without_a_score():
    """A hit with no prominence score is worth nothing, not an error."""
    assert parse_nominatim_hit({"lat": "1", "lon": "2"}).importance == 0.0
    assert parse_nominatim_hit({"lat": "1", "lon": "2", "importance": "x"}).importance == 0.0


@pytest.mark.asyncio
async def test_geocoder_returns_first_hit():
    """A resolved name comes back as a point, and the lookup is cached."""
    geocoder = Geocoder()
    response = MagicMock()
    response.read.return_value = json.dumps([NOMINATIM_HIT]).encode()
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *args: False

    with patch("src.enrichment.geocoder.urllib.request.urlopen", return_value=response) as urlopen:
        first = await geocoder.geocode("New York City")
        second = await geocoder.geocode("new york city")

    assert (first.latitude, first.longitude) == (40.7128, -74.0060)
    assert second == first
    assert urlopen.call_count == 1


@pytest.mark.asyncio
async def test_geocoder_returns_none_for_no_match():
    """A name Nominatim does not know resolves to nothing, not an error."""
    geocoder = Geocoder()
    response = MagicMock()
    response.read.return_value = b"[]"
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *args: False

    with patch("src.enrichment.geocoder.urllib.request.urlopen", return_value=response):
        assert await geocoder.geocode("Flibbertigibbet") is None


@pytest.mark.asyncio
async def test_geocoder_survives_a_broken_provider():
    """An unreachable geocoder returns None and is not retried in the same run."""
    geocoder = Geocoder()

    with patch(
        "src.enrichment.geocoder.urllib.request.urlopen",
        side_effect=OSError("connection refused"),
    ) as urlopen:
        assert await geocoder.geocode("Iran") is None
        assert await geocoder.geocode("Iran") is None

    assert urlopen.call_count == 1


@pytest.mark.asyncio
async def test_geocoder_ignores_blank_names():
    """A blank name never reaches the network."""
    geocoder = Geocoder()

    with patch("src.enrichment.geocoder.urllib.request.urlopen") as urlopen:
        assert await geocoder.geocode("   ") is None

    urlopen.assert_not_called()


# --- backfill ---------------------------------------------------------------


def located(name, latitude, longitude, location_type="city", importance=0.0):
    return CanonicalEntity(
        id=uuid.uuid4(),
        canonical_name=name,
        entity_type="GPE",
        latitude=latitude,
        longitude=longitude,
        location_type=location_type,
        geo_importance=importance,
    )


def test_most_specific_prefers_the_finest_place():
    """A story naming a country and one of its cities dots the city."""
    country = located("United States", 39.8, -98.6, "country")
    state = located("Alabama", 32.8, -86.8, "state")
    city = located("Birmingham", 33.5, -86.8, "city")

    assert most_specific([country, city]).canonical_name == "Birmingham"
    assert most_specific([country, state]).canonical_name == "Alabama"
    assert most_specific([state, city]).canonical_name == "Birmingham"


def test_most_specific_prefers_the_match_the_geocoder_was_sure_about():
    """Equal granularity is broken by match prominence, not by the alphabet.

    The real case: "Man City" resolves to a village in Cote d'Ivoire and
    "Manchester City" to Manchester. Both come back as "city", so only the
    prominence score separates the football club from West Africa.
    """
    man_city = located("Man City", 7.4103, -7.5504, "city", importance=0.34)
    manchester = located("Manchester City", 53.4795, -2.2451, "city", importance=0.74)

    assert most_specific([man_city, manchester]).canonical_name == "Manchester City"
    assert most_specific([manchester, man_city]).canonical_name == "Manchester City"


def test_most_specific_is_deterministic():
    """With nothing to separate them, the name decides, and it always does."""
    first = located("Alabama", 32.8, -86.8, "state")
    second = located("Minnesota", 46.3, -94.3, "state")

    assert most_specific([first, second]).canonical_name == "Alabama"
    assert most_specific([second, first]).canonical_name == "Alabama"


def test_most_specific_ranks_unknown_labels_last():
    """A granularity we do not know never wins against one we do."""
    known = located("Tel Aviv", 32.08, 34.78, "city")
    unknown = located("Somewhere", 0.0, 0.0, "wormhole")

    assert most_specific([unknown, known]).canonical_name == "Tel Aviv"
    assert all(UNKNOWN_SPECIFICITY > rank for rank in PLACE_SPECIFICITY.values())


def test_story_event_copies_the_storys_own_numbers():
    """The event carries the story's sourcing, not invented confidence."""
    story = Story(
        id=uuid.uuid4(),
        day=datetime(2026, 10, 1, tzinfo=timezone.utc),
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=1,
    )
    entity = located("Tel Aviv", 32.0853, 34.7818, "city")

    event = story_event(story, entity)

    assert event.story_id == story.id
    assert event.latitude == 32.0853
    assert event.longitude == 34.7818
    assert event.location_name == "Tel Aviv"
    assert event.location_type == "city"
    assert event.start_time == story.day
    # Nothing classifies the event, so nothing claims to know its type.
    assert event.event_type == Event.EventType.OTHER
    assert event.source_count == 4
    assert event.tier1_source_count == 2
    assert event.confidence == 0.5
    assert event.entities == {"GPE": ["Tel Aviv"]}
    assert event.layer_id is None


def test_story_event_keeps_coordinates_off_null_island():
    """An entity on the equator or the prime meridian is still a real point."""
    story = Story(id=uuid.uuid4(), day=datetime(2026, 10, 1, tzinfo=timezone.utc))

    event = story_event(story, located("Ghana", 0.0, 0.0, "country"))

    assert event.latitude == 0.0
    assert event.longitude == 0.0
    assert event.confidence == 0.0


def test_group_by_story_collapses_a_storys_entities():
    """One story with three located entities is one story, not three."""
    story = Story(id=uuid.uuid4(), day=datetime(2026, 10, 1, tzinfo=timezone.utc))
    rows = [(story, located("A", 1.0, 1.0)), (story, located("B", 2.0, 2.0))]

    grouped = group_by_story(rows)

    assert list(grouped) == [story.id]
    assert len(grouped[story.id][1]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
