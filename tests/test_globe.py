"""Tests for globe visualization functionality."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.schema.models import (
    Event, EventGeometry, EventLayer
)
from src.enrichment.geocoder import Geocoder, GeoResult
from src.enrichment.event_locator import (
    classify_event_type,
    extract_location_candidates,
)


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


def test_classify_event_type():
    """Test event type classification from keywords."""
    # Conflict
    event_type, confidence = classify_event_type("War breaks out as troops clash in battle with artillery strikes")
    assert event_type == "conflict"
    assert confidence > 0.5

    # Protest (3 keywords: protest, demonstration, rally)
    event_type, confidence = classify_event_type("Thousands protest in demonstration rally against policy")
    assert event_type == "protest"
    assert confidence > 0.5

    # Election
    event_type, confidence = classify_event_type("Election day as voters cast ballots in presidential vote")
    assert event_type == "election"
    assert confidence > 0.5

    # Disaster
    event_type, confidence = classify_event_type("Earthquake strikes city causing flood and landslide damage")
    assert event_type == "disaster"
    assert confidence > 0.5

    # Unknown
    event_type, confidence = classify_event_type("Random text with no event keywords")
    assert event_type == "other"
    assert confidence == 0.3


def test_extract_location_candidates():
    """Test location candidate extraction from text and entities."""
    text = "The protest in Paris yesterday drew thousands. Officials in New York City responded."
    entities = {
        "GPE": ["Paris", "New York City"],
        "LOC": ["France"],
        "PERSON": ["John"],
    }

    candidates = extract_location_candidates(text, entities)

    assert "Paris" in candidates
    assert "New York City" in candidates
    assert "France" in candidates


def test_geocoder_result():
    """Test GeoResult dataclass."""

    result = GeoResult(
        name="New York City",
        latitude=40.7128,
        longitude=-74.0060,
        location_type="city",
        country_code="US",
        admin1="New York",
        geonames_id="5128581",
    )
    assert result.latitude == 40.7128
    assert result.longitude == -74.0060
    assert result.country_code == "US"


@pytest.mark.asyncio
async def test_geocoder_nominatim():
    """Test Nominatim geocoding (mocked)."""

    geocoder = Geocoder()

    # Mock the HTTP client
    with patch('httpx.AsyncClient.get') as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = [{
            "display_name": "New York City, New York, USA",
            "lat": "40.7128",
            "lon": "-74.0060",
            "type": "city",
            "address": {"country_code": "us", "state": "New York"},
        }]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        results = await geocoder.geocode_nominatim("New York City", limit=1)

        assert len(results) == 1
        assert results[0].name == "New York City, New York, USA"
        assert results[0].latitude == 40.7128
        assert results[0].longitude == -74.0060
        assert results[0].location_type == "city"
        assert results[0].country_code == "US"


@pytest.mark.asyncio
async def test_classify_event_type_llm():
    """Test LLM-based event classification (mocked)."""
    from src.enrichment.event_locator import infer_event_type_llm

    with patch('src.enrichment.event_locator.get_llm_client') as mock_get_llm:
        mock_client = AsyncMock()
        mock_get_llm.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"event_type": "conflict", "confidence": 95, "reasoning": "Clear military action"}'
        mock_client.chat_completion.return_value = mock_response

        event_type, confidence = await infer_event_type_llm(
            "War breaks out as troops advance with artillery",
            "War Breaks Out"
        )

        assert event_type == "conflict"
        assert confidence == 0.95


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


def test_event_radius_estimation():
    """Test event radius estimation by location type."""
    from src.enrichment.event_locator import _estimate_radius

    assert _estimate_radius("point") == 1.0
    assert _estimate_radius("city") == 25.0
    assert _estimate_radius("town") == 10.0
    assert _estimate_radius("village") == 5.0
    assert _estimate_radius("country") == 500.0
    assert _estimate_radius("state") == 100.0
    assert _estimate_radius("region") == 200.0
    assert _estimate_radius("unknown") == 25.0  # Default


def test_canonical_entity_geolocation():
    """Test CanonicalEntity with geolocation fields."""
    from src.schema.models import CanonicalEntity

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


@pytest.mark.asyncio
async def test_enrich_story_with_event():
    """Test story enrichment with event (mocked)."""
    from src.enrichment.event_locator import enrich_story_with_event

    mock_session = AsyncMock()

    # Mock the story query
    mock_story = MagicMock()
    mock_story.id = uuid.uuid4()
    mock_story.story_ids = []

    mock_session.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_story
    mock_session.execute.return_value = mock_result

    # Mock create_event_from_story
    with patch('src.enrichment.event_locator.create_event_from_story', new_callable=AsyncMock) as mock_create:
        mock_create.return_value = None  # No event created

        result = await enrich_story_with_event(mock_session, str(uuid.uuid4()))
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])