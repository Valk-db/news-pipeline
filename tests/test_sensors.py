"""Tests for the sensor feeds: USGS earthquakes and GDACS alerts.

Fixtures are hand written to match the verified live shapes. No network calls.
"""

import json
from datetime import UTC

import pytest

from src.ingestion import sensors


# ------------------------------------------------------------------ USGS

USGS_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "mag": 5.4,
                "place": "120 km SSW of Kyiv, Ukraine",
                "time": 1759248000000,
                "updated": 1759248300000,
                "tz": "UTC",
                "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/us7000abcd",
                "detail": "https://earthquake.usgs.gov/fdsnws/event/1/detail/us7000abcd",
                "felt": 12,
                "cdi": 4,
                "mmi": 3,
                "alert": "green",
                "status": "reviewed",
                "tsunami": 0,
                "net": "us",
                "code": "7000abcd",
                "ids": "us7000abcd",
                "sources": "us,pt",
                "title": "M 5.4 - 120 km SSW of Kyiv, Ukraine",
                "type": "earthquake",
            },
            "geometry": {
                "type": "Point",
                # GeoJSON is [long, lat]
                "coordinates": [30.5234, 50.4501],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "mag": 2.1,
                "place": "10 km N of Somewhere, Iceland",
                "time": 1759240000000,
                "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/us7000efgh",
                "title": "M 2.1 - 10 km N of Somewhere, Iceland",
                "net": "us",
            },
            "geometry": {"type": "Point", "coordinates": [-21.9, 64.1]},
        },
        {
            "type": "Feature",
            "properties": {
                "mag": 3.0,
                "place": "Broken coordinates",
                "time": 1759240000000,
                "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/us7000bad",
                "net": "us",
            },
            # Lat/lon out of range, so the feature has to be skipped
            "geometry": {"type": "Point", "coordinates": [500.0, 200.0]},
        },
    ],
}


class TestUsgsParsing:
    def test_parses_features_into_articles(self):
        articles, skipped = sensors.parse_usgs_geojson(json.dumps(USGS_FIXTURE))

        assert len(articles) == 2
        assert skipped == 1

    def test_article_shape_matches_adapter_contract(self):
        articles, _ = sensors.parse_usgs_geojson(json.dumps(USGS_FIXTURE))
        art = articles[0]

        for key in (
            "url", "url_hash", "title", "body_text", "summary",
            "source_domain", "source_tier", "published_at", "entities",
            "content_hash",
        ):
            assert key in art

        assert art["source_domain"] == "earthquake.usgs.gov"
        assert art["url"].startswith("https://earthquake.usgs.gov/")

    def test_coordinates_are_lat_lon_not_lon_lat(self):
        articles, _ = sensors.parse_usgs_geojson(json.dumps(USGS_FIXTURE))
        geo = articles[0]["entities"]["GEO"]
        # The fixture geometry is [30.5234, 50.4501] as long, lat
        assert geo["lat"] == pytest.approx(50.4501)
        assert geo["lon"] == pytest.approx(30.5234)

    def test_epoch_ms_becomes_utc_datetime(self):
        articles, _ = sensors.parse_usgs_geojson(json.dumps(USGS_FIXTURE))
        assert articles[0]["published_at"].tzinfo == UTC
        assert articles[0]["published_at"].year == 2025

    def test_body_mentions_magnitude_and_place(self):
        articles, _ = sensors.parse_usgs_geojson(json.dumps(USGS_FIXTURE))
        body = articles[0]["body_text"]
        assert "5.4" in body
        assert "Kyiv" in body
        assert "magnitude" in body

    def test_title_falls_back_when_absent(self):
        payload = {
            "features": [{
                "properties": {"mag": 4.2, "place": "Off Coast", "time": 1759248000000,
                               "url": "https://earthquake.usgs.gov/x", "net": "us"},
                "geometry": {"type": "Point", "coordinates": [10.0, 20.0]},
            }]
        }
        articles, _ = sensors.parse_usgs_geojson(json.dumps(payload))
        assert "magnitude 4.2" in articles[0]["title"]
        assert "Off Coast" in articles[0]["title"]

    def test_empty_feature_collection(self):
        articles, skipped = sensors.parse_usgs_geojson(json.dumps({"features": []}))
        assert articles == []
        assert skipped == 0

    def test_malformed_payload_raises_value_error(self):
        with pytest.raises(ValueError):
            sensors.parse_usgs_geojson("not json")

    def test_network_failure_returns_empty_list(self):
        # The feed functions log and return [] rather than raising, so a dead
        # sensor cannot take the run down.
        import urllib.error

        def boom(url):
            raise urllib.error.URLError("unreachable")

        original = sensors._http_get
        sensors._http_get = boom
        try:
            assert sensors.usgs_earthquakes() == []
        finally:
            sensors._http_get = original


# ----------------------------------------------------------------- GDACS

# Shapes taken from the live feed: geo:lat and geo:long sit inside a
# geo:Point wrapper, georss:point is a sibling fallback, and gdacs:severity
# carries attributes alongside its text.
GDACS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"
     xmlns:gdacs="http://www.gdacs.org"
     xmlns:georss="http://www.georss.org/georss">
  <channel>
    <title>GDACS RSS information</title>
    <item>
      <title>Orange earthquake alert in Ukraine</title>
      <description>&lt;b&gt;Magnitude 6.1&lt;/b&gt; earthquake near Kyiv.</description>
      <link>https://www.gdacs.org/report.aspx?eventtype=EQ&amp;eventid=1568826</link>
      <pubDate>Thu, 01 Oct 2026 12:00:00 GMT</pubDate>
      <guid isPermaLink="false">EQ1568826</guid>
      <geo:Point>
        <geo:lat>50.45</geo:lat>
        <geo:long>30.52</geo:long>
      </geo:Point>
      <georss:point>50.45 30.52</georss:point>
      <gdacs:eventtype>EQ</gdacs:eventtype>
      <gdacs:alertlevel>Orange</gdacs:alertlevel>
      <gdacs:severity unit="" value="6.1">Magnitude 6.1 </gdacs:severity>
      <gdacs:eventid>1568826</gdacs:eventid>
      <gdacs:country>Ukraine</gdacs:country>
      <gdacs:iso3>UKR</gdacs:iso3>
    </item>
    <item>
      <title>Green tropical cyclone alert in the Philippines</title>
      <description>Cyclone warning.</description>
      <link>https://www.gdacs.org/report.aspx?eventtype=TC&amp;eventid=2000001</link>
      <pubDate>Thu, 01 Oct 2026 11:00:00 GMT</pubDate>
      <guid isPermaLink="false">TC2000001</guid>
      <georss:point>14.6 121.0</georss:point>
      <gdacs:eventtype>TC</gdacs:eventtype>
      <gdacs:alertlevel>Green</gdacs:alertlevel>
      <gdacs:severity unit="" value="0">Magnitude 0 </gdacs:severity>
      <gdacs:eventid>2000001</gdacs:eventid>
      <gdacs:country>Philippines</gdacs:country>
      <gdacs:iso3>PHL</gdacs:iso3>
    </item>
    <item>
      <title>Green flood alert with no coordinates</title>
      <description>No geo fields here.</description>
      <link>https://www.gdacs.org/report.aspx?eventtype=FL&amp;eventid=3000002</link>
      <pubDate>Thu, 01 Oct 2026 10:00:00 GMT</pubDate>
      <guid isPermaLink="false">FL3000002</guid>
      <gdacs:eventtype>FL</gdacs:eventtype>
      <gdacs:eventid>3000002</gdacs:eventid>
    </item>
  </channel>
</rss>
"""


class TestGdacsParsing:
    def test_parses_items_into_articles(self):
        articles, skipped = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        assert len(articles) == 2
        assert skipped == 1

    def test_article_shape_matches_adapter_contract(self):
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        art = articles[0]
        for key in (
            "url", "url_hash", "title", "body_text", "summary",
            "source_domain", "source_tier", "published_at", "entities",
            "content_hash",
        ):
            assert key in art
        assert art["source_domain"] == "gdacs.org"

    def test_geo_lat_long_are_used(self):
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        geo = articles[0]["entities"]["GEO"]
        assert geo["lat"] == pytest.approx(50.45)
        assert geo["lon"] == pytest.approx(30.52)

    def test_georss_point_is_fallback(self):
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        # The second item has no geo:lat/geo:long, only georss:point
        assert len(articles) == 2
        geo = articles[1]["entities"]["GEO"]
        # georss:point is "lat lon"
        assert geo["lat"] == pytest.approx(14.6)
        assert geo["lon"] == pytest.approx(121.0)

    def test_gdacs_fields_land_in_geo_and_body(self):
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        geo = articles[0]["entities"]["GEO"]
        assert geo["alert_level"] == "Orange"
        assert geo["event_type"] == "EQ"
        assert geo["event_id"] == "EQ1568826"
        assert "Orange" in articles[0]["body_text"]
        assert "Ukraine" in articles[0]["body_text"]

    def test_geo_point_wrapper_is_searched(self):
        # geo:lat/geo:long are nested in geo:Point on the live feed, so a
        # direct child scan would drop the coordinates.
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        assert articles[0]["entities"]["GEO"]["lat"] is not None

    def test_pubdate_becomes_utc_datetime(self):
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        assert articles[0]["published_at"].tzinfo == UTC
        assert articles[0]["published_at"].year == 2026

    def test_html_in_description_is_stripped(self):
        articles, _ = sensors.parse_gdacs_rss(GDACS_FIXTURE)
        assert "<b>" not in articles[0]["body_text"]
        assert "Magnitude 6.1" in articles[0]["body_text"]

    def test_malformed_xml_returns_empty(self):
        articles, skipped = sensors.parse_gdacs_rss("<rss><channel><item>")
        assert articles == []
        assert skipped == 0

    def test_network_failure_returns_empty_list(self):
        import urllib.error

        def boom(url):
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)

        original = sensors._http_get
        sensors._http_get = boom
        try:
            assert sensors.gdacs_alerts() == []
        finally:
            sensors._http_get = original


# --------------------------------------------------------------- adapter

class TestSensorAdapter:
    @pytest.mark.asyncio
    async def test_fetch_combines_feeds(self):
        from src.ingestion.adapters.sensor_adapter import SensorAdapter

        def fake_usgs():
            return [{
                "url": "https://earthquake.usgs.gov/x",
                "url_hash": "h1",
                "title": "Quake",
                "body_text": "body",
                "summary": "body",
                "source_domain": "earthquake.usgs.gov",
                "source_tier": "tier3",
                "published_at": None,
                "entities": {"GEO": {"lat": 1.0, "lon": 2.0, "name": "x"}},
                "content_hash": "c1",
            }]

        def fake_gdacs():
            return [{
                "url": "https://www.gdacs.org/y",
                "url_hash": "h2",
                "title": "Flood",
                "body_text": "body",
                "summary": "body",
                "source_domain": "gdacs.org",
                "source_tier": "tier3",
                "published_at": None,
                "entities": {"GEO": {"lat": 3.0, "lon": 4.0, "name": "y"}},
                "content_hash": "c2",
            }]

        original_usgs = sensors.usgs_earthquakes
        original_gdacs = sensors.gdacs_alerts
        sensors.usgs_earthquakes = fake_usgs
        sensors.gdacs_alerts = fake_gdacs
        try:
            adapter = SensorAdapter()
            articles = await adapter.fetch()
        finally:
            sensors.usgs_earthquakes = original_usgs
            sensors.gdacs_alerts = original_gdacs

        assert len(articles) == 2
        assert {a.source_domain for a in articles} == {
            "earthquake.usgs.gov", "gdacs.org",
        }
        assert all(str(a.source_tier) == "SourceTier.TIER3" for a in articles)

    @pytest.mark.asyncio
    async def test_one_dead_feed_does_not_sink_the_run(self):
        from src.ingestion.adapters.sensor_adapter import SensorAdapter

        def boom():
            raise RuntimeError("feed exploded")

        def fake_gdacs():
            return [{
                "url": "https://www.gdacs.org/y",
                "url_hash": "h2",
                "title": "Flood",
                "body_text": "body",
                "source_domain": "gdacs.org",
                "source_tier": "tier3",
                "entities": {},
                "content_hash": "c2",
            }]

        original_usgs = sensors.usgs_earthquakes
        original_gdacs = sensors.gdacs_alerts
        sensors.usgs_earthquakes = boom
        sensors.gdacs_alerts = fake_gdacs
        try:
            adapter = SensorAdapter()
            articles = await adapter.fetch()
            health = await adapter.health_check()
        finally:
            sensors.usgs_earthquakes = original_usgs
            sensors.gdacs_alerts = original_gdacs

        assert len(articles) == 1
        assert health.status == "degraded"
        assert "usgs_earthquakes" in health.failed

    @pytest.mark.asyncio
    async def test_health_down_before_fetch(self):
        from src.ingestion.adapters.sensor_adapter import SensorAdapter

        health = await SensorAdapter().health_check()
        assert health.status == "down"
