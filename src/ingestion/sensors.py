"""Sensor and hazard feeds as article shaped records.

Two public, credential free feeds, both verified live:

  USGS earthquakes: https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson
      about 290 features per day. Feature properties carry mag, place, time
      (epoch ms), url, title. Geometry is a Point as [long, lat].

  GDACS: https://www.gdacs.org/xml/rss.xml
      about 223 items. Each item has title, description, link, pubDate,
      geo:lat, geo:long, gdacs:alertlevel, gdacs:severity, gdacs:eventtype,
      gdacs:country, gdacs:iso3 and a guid such as EQ1568826. The other GDACS
      endpoints tried were admin pages or 404, so this feed is the only one.

Both functions return article shaped dicts matching what the ingestion
adapters consume: url, title, body_text, summary, published_at,
source_domain, and coordinates in entities["GEO"] with lat and lon, the same
field the GDELT paths already populate.

Network access is urllib only, so the egress proxy is honored.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.utils.trafilatura_extract import compute_content_hash, compute_url_hash

logger = logging.getLogger(__name__)


USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"

USER_AGENT = "news-pipeline/0.1 (hazard sensor reader)"
HTTP_TIMEOUT_SECONDS = 60

# Sensors are machine feeds rather than editorial outlets, and the registry
# lists them as tier 3.
SOURCE_TIER = "tier3"

GEO_NS = "http://www.georss.org/georss"
GDACS_NS = "gdacs"

WHITESPACE_RE = re.compile(r"\s+")

# USGS magnitude bands, used for the title and body wording.
MAGNITUDE_BANDS = (
    (7.0, "Major"),
    (6.0, "Strong"),
    (5.0, "Moderate"),
    (4.0, "Light"),
    (3.0, "Minor"),
    (0.0, "Very small"),
)

GDACS_EVENT_TYPES = {
    "EQ": "Earthquake",
    "FL": "Flood",
    "TC": "Tropical Cyclone",
    "VO": "Volcano",
    "DR": "Drought",
    "WF": "Wildfire",
    "TS": "Tsunami",
}


def _http_get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.read()


def _clean(text: Optional[str]) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()


def _magnitude_band(mag: Optional[float]) -> str:
    if mag is None:
        return "Unrated"
    for threshold, label in MAGNITUDE_BANDS:
        if mag >= threshold:
            return label
    return "Very small"


def _valid_coords(lat: Optional[float], lon: Optional[float]) -> Optional[Tuple[float, float]]:
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms_to_datetime(value) -> Optional[datetime]:
    ms = _to_float(value)
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_pubdate(value: str) -> Optional[datetime]:
    """Parse an RFC 822 pubDate, as GDACS emits."""
    value = _clean(value)
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(value)
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, IndexError):
        return None


def _geo_entities(name: str, lat: float, lon: float, **extra) -> Dict:
    geo = {"name": name, "lat": lat, "lon": lon}
    geo.update({k: v for k, v in extra.items() if v not in (None, "")})
    return {"GEO": geo}


def _article(
    url: str,
    title: str,
    body_text: str,
    published_at: Optional[datetime],
    source_domain: str,
    entities: Dict,
    source_name: str,
) -> Dict:
    """Assemble one article shaped dict in the adapter article shape."""
    return {
        "url": url,
        "url_hash": compute_url_hash(url),
        "title": title,
        "body_text": body_text,
        "summary": body_text[:500],
        "source_domain": source_domain,
        "source_tier": SOURCE_TIER,
        "published_at": published_at,
        "entities": entities,
        "content_hash": compute_content_hash(body_text),
        "source_name": source_name,
    }


# ------------------------------------------------------------------- USGS


def parse_usgs_geojson(payload: str) -> Tuple[List[Dict], int]:
    """Parse the USGS all_day GeoJSON into article dicts.

    Returns (articles, skipped) where skipped counts features with no usable
    coordinates. GeoJSON Point coordinates are [long, lat]; the order is the
    reverse of our lat/lon field names, so this is swapped explicitly.
    """
    data = json.loads(payload)
    articles: List[Dict] = []
    skipped = 0
    seen_urls = set()

    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []

        url = _clean(props.get("url"))
        if not url:
            skipped += 1
            continue

        # Point is [long, lat]; QuakeML detail features carry depth first.
        if geometry.get("type") == "Point" and len(coordinates) >= 2:
            lon = _to_float(coordinates[0])
            lat = _to_float(coordinates[1])
        else:
            lat = _to_float(props.get("lat"))
            lon = _to_float(props.get("lon"))
        coords = _valid_coords(lat, lon)
        if coords is None:
            skipped += 1
            continue

        url_hash = compute_url_hash(url)
        if url_hash in seen_urls:
            continue
        seen_urls.add(url_hash)

        mag = _to_float(props.get("mag"))
        place = _clean(props.get("place")) or "Unknown location"
        published_at = _epoch_ms_to_datetime(props.get("time"))
        band = _magnitude_band(mag)
        mag_text = f"{mag:.1f}" if mag is not None else "unknown"

        title = _clean(props.get("title")) or f"{band} earthquake, magnitude {mag_text}, {place}"

        parts = [
            f"A {band.lower()} earthquake of magnitude {mag_text} was reported near {place}.",
        ]
        if published_at is not None:
            parts.append(f"Reported at {published_at.isoformat()}.")
        parts.append(
            f"Coordinates {coords[0]:.4f} latitude, {coords[1]:.4f} longitude. "
            f"Source USGS Earthquakes all_day feed."
        )
        if props.get("tsunami"):
            parts.append("The event is flagged as tsunami relevant.")
        if props.get("alert"):
            parts.append(f"PAGER alert level: {props['alert']}.")
        if props.get("felt"):
            parts.append(f"Felt reports: {props['felt']}.")
        if props.get("cdi"):
            parts.append(f"Maximum reported intensity: {props['cdi']}.")
        parts.append(f"Detail page: {url}")
        body_text = " ".join(parts)

        entities = _geo_entities(
            place,
            coords[0],
            coords[1],
            country=_clean(props.get("net")) or "usgs",
            source="usgs",
            magnitude=mag,
            alert=_clean(props.get("alert")),
        )

        articles.append(_article(
            url=url,
            title=title,
            body_text=body_text,
            published_at=published_at,
            source_domain="earthquake.usgs.gov",
            entities=entities,
            source_name="USGS Earthquakes",
        ))

    return articles, skipped


def usgs_earthquakes() -> List[Dict]:
    """Fetch the USGS all_day earthquake feed and return article dicts.

    A transport failure returns an empty list and logs, so one dead sensor does
    not take down the run.
    """
    try:
        payload = _http_get(USGS_URL).decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as e:
        logger.warning("USGS earthquake feed unavailable: %s", e)
        return []

    try:
        articles, skipped = parse_usgs_geojson(payload)
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("USGS earthquake feed unparsable: %s", e)
        return []

    logger.info("USGS earthquakes: %d articles, %d skipped for missing coordinates", len(articles), skipped)
    return articles


# ------------------------------------------------------------------ GDACS


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return _clean(text)


def parse_gdacs_rss(payload: str) -> Tuple[List[Dict], int]:
    """Parse the GDACS RSS feed into article dicts.

    Namespace agnostic on purpose: the feed mixes georss, gdacs and default
    namespaces, and the tag local names are stable. Returns (articles, skipped)
    where skipped counts items with no usable coordinates.
    """
    articles: List[Dict] = []
    skipped = 0
    seen_urls = set()

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as e:
        logger.warning("GDACS feed unparsable XML: %s", e)
        return [], 0

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def find_child(node, name: str):
        for child in node:
            if local(child.tag) == name:
                return child
        return None

    def find_text(node, name: str) -> Optional[str]:
        child = find_child(node, name)
        return _clean(child.text) if child is not None else None

    def find_descendant_text(node, name: str) -> Optional[str]:
        """Search the whole subtree.

        The live feed nests geo:lat and geo:long inside a geo:Point wrapper, so
        a direct child scan misses them on the newer items.
        """
        for child in node:
            if local(child.tag) == name:
                return _clean(child.text)
            found = find_descendant_text(child, name)
            if found:
                return found
        return None

    channel = root if local(root.tag) == "rss" else None
    if channel is not None:
        for child in root:
            if local(child.tag) == "channel":
                channel = child
                break

    items = [
        node for node in (channel if channel is not None else root)
        if local(node.tag) == "item"
    ]

    for item in items:
        guid = find_text(item, "guid") or ""
        title = find_text(item, "title") or ""
        link = find_text(item, "link") or ""
        description = _strip_html(find_text(item, "description") or "")
        pub_date = _parse_pubdate(find_text(item, "pubdate") or "")

        event_type = (find_text(item, "eventtype") or "").upper()
        alert_level = find_text(item, "alertlevel") or ""
        severity = find_text(item, "severity") or ""
        country = find_text(item, "country") or ""
        iso3 = find_text(item, "iso3") or ""

        # geo:lat and geo:long sit inside a geo:Point wrapper on the live feed.
        lat = _to_float(find_descendant_text(item, "lat"))
        lon = _to_float(find_descendant_text(item, "long"))
        if coords_hint := find_descendant_text(item, "point"):
            # georss:point is "lat lon", a fallback when geo:lat/geo:long are absent.
            bits = coords_hint.split()
            if len(bits) >= 2:
                lat = lat if lat is not None else _to_float(bits[0])
                lon = lon if lon is not None else _to_float(bits[1])
        coords = _valid_coords(lat, lon)
        if coords is None:
            skipped += 1
            continue

        if not link:
            link = f"https://www.gdacs.org/report.aspx?eventtype={event_type}&eventid={guid}"

        url_hash = compute_url_hash(link)
        if url_hash in seen_urls:
            continue
        seen_urls.add(url_hash)

        type_label = GDACS_EVENT_TYPES.get(event_type, event_type or "Event")
        if not title:
            title = f"{type_label} {guid}".strip()

        parts = [
            f"GDACS reported a {type_label.lower()} event ({guid or 'no id'}).",
        ]
        if alert_level:
            parts.append(f"Alert level: {alert_level}.")
        if severity:
            parts.append(f"Severity: {severity}.")
        if country:
            parts.append(f"Country: {country}{f' ({iso3})' if iso3 else ''}.")
        parts.append(f"Coordinates {coords[0]:.4f} latitude, {coords[1]:.4f} longitude.")
        if pub_date is not None:
            parts.append(f"Published at {pub_date.isoformat()}.")
        if description:
            parts.append(description)
        parts.append(f"Detail page: {link}")
        body_text = " ".join(parts)

        entities = _geo_entities(
            title,
            coords[0],
            coords[1],
            country=country or iso3,
            source="gdacs",
            event_type=event_type,
            alert_level=alert_level,
            severity=severity,
            event_id=guid,
        )

        articles.append(_article(
            url=link,
            title=title,
            body_text=body_text,
            published_at=pub_date,
            source_domain="gdacs.org",
            entities=entities,
            source_name="GDACS",
        ))

    return articles, skipped


def gdacs_alerts() -> List[Dict]:
    """Fetch the GDACS RSS feed and return article dicts.

    A transport failure returns an empty list and logs rather than raising.
    """
    try:
        payload = _http_get(GDACS_URL).decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as e:
        logger.warning("GDACS feed unavailable: %s", e)
        return []

    articles, skipped = parse_gdacs_rss(payload)
    logger.info("GDACS alerts: %d articles, %d skipped for missing coordinates", len(articles), skipped)
    return articles
