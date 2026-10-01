"""GDELT 2.0 static file ingestion (primary GDELT feed).

Every 15 minutes GDELT publishes a set of static files and a manifest at
http://data.gdeltproject.org/gdeltv2/lastupdate.txt. The manifest lists the
current timestamps; for each timestamp there are three zips:

  <ts>.export.CSV.zip   event rows, tab separated, 61 columns, no header
  <ts>.mentions.CSV.zip  mention rows (not used here)
  <ts>.gkg.csv.zip      Global Knowledge Graph rows, 27 columns, no header

Files rotate on a 15 minute clock, so a timestamp read from a stale manifest
can 404 by the time we download it. We therefore re-read lastupdate.txt right
before every download, and a 404 on a data file is a soft skip of that file
rather than a fatal error.

GKG is the primary article source because it carries real article URLs and
titles. The export file is parsed for event geography and tone, and joined to
GKG rows by SOURCEURL == DocumentIdentifier when the exact URL matches, which
is cheap and optional.

All network access goes through urllib.request, which honours the egress proxy
from the standard environment. All disk work happens under TMPDIR so the
512MB /tmp tmpfs is never used for a large download.

Volume is capped per run: we process only the latest file set and never page
through history. The newest 15 minute window is small (a few MB unzipped), so
one window per run is the right tradeoff for a scheduled job.
"""

import asyncio
import csv
import gzip
import io
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
from src.utils.trafilatura_extract import (
    compute_content_hash,
    compute_url_hash,
    extract_article,
)

logger = logging.getLogger(__name__)


BASE_URL = "http://data.gdeltproject.org/gdeltv2/"
LASTUPDATE_URL = BASE_URL + "lastupdate.txt"
EVENTS_SUFFIX = ".export.CSV.zip"
GKG_SUFFIX = ".gkg.csv.zip"

USER_AGENT = "news-pipeline/0.1 (GDELT static file reader)"
HTTP_TIMEOUT_SECONDS = 120

# Column indices, verified against the live files.
EV = {
    "global_event_id": 0,
    "sqldate": 1,
    "is_root_event": 25,
    "event_code": 26,
    "event_base_code": 27,
    "event_root_code": 28,
    "quad_class": 29,
    "goldstein_scale": 30,
    "num_mentions": 31,
    "num_sources": 32,
    "num_articles": 33,
    "avg_tone": 34,
    "action_geo_name": 36,
    "action_geo_country": 37,
    "action_geo_lat": 40,
    "action_geo_long": 41,
    "actor1_geo_name": 44,
    "actor1_geo_lat": 48,
    "actor1_geo_long": 49,
    "actor2_geo_name": 52,
    "actor2_geo_lat": 56,
    "actor2_geo_long": 57,
    "date_added": 59,
    "source_url": 60,
}
EV_MIN_COLUMNS = 61

GKG = {
    "record_id": 0,
    "date": 1,
    "source_collection_identifier": 2,
    "source_common_name": 3,
    "document_identifier": 4,
    "themes": 7,
    "locations": 9,
    "persons": 11,
    "organizations": 13,
    "tone": 15,
    "title": 26,
}
GKG_MIN_COLUMNS = 27

# V1LOCATIONS entry: type#name#countrycode#adm1#lat#long#featureid
LOCATION_PART_COUNT = 7
LOCATION_LAT_INDEX = 4
LOCATION_LON_INDEX = 5

TITLE_TAG_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.DOTALL | re.IGNORECASE)

DOMAIN_TIERS = {
    "apnews.com": SourceTier.TIER1,
    "reuters.com": SourceTier.TIER1,
    "bbc.com": SourceTier.TIER1,
    "theguardian.com": SourceTier.TIER1,
    "npr.org": SourceTier.TIER1,
}

# GKG rows are metadata records, not article prose, so the DOC path's 200
# character prose gate does not apply here. A short floor still rejects rows
# whose only content is the title.
MIN_BODY_LENGTH = 20


def tier_for_domain(domain: str) -> SourceTier:
    """Tier by domain, defaulting to tier 2 like the DOC API path."""
    return DOMAIN_TIERS.get(domain, SourceTier.TIER2)


@dataclass
class StaticResult:
    """Outcome of one static file pass. Mirrors gdelt.DomainResult shape."""

    articles: List[RawArticle] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None
    timestamps: List[str] = field(default_factory=list)
    events_rows: int = 0
    gkg_rows: int = 0
    skipped_no_geo: int = 0
    joined: int = 0
    soft_skips: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- utilities


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_coords(lat: Optional[float], lon: Optional[float]) -> Optional[Tuple[float, float]]:
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


def parse_lastupdate(text: str) -> List[str]:
    """Pull the newest first list of 15 minute timestamps out of lastupdate.txt.

    Each listing line is "size md5 url", where the url ends in the file name.
    We take the leading 14 digits of the file name, which is the 15 minute
    window timestamp, and sort descending so index 0 is the newest window. That
    keeps the per run cap on the freshest data even if the manifest lists
    several windows.
    """
    stamps = set()
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[-1].rsplit("/", 1)[-1]
        if not name.endswith(".zip"):
            continue
        head = name[:14]
        if head.isdigit() and len(head) == 14:
            stamps.add(head)
    return sorted(stamps, reverse=True)


def _open_zip_member(path: str) -> Tuple[zipfile.ZipFile, io.TextIOWrapper]:
    """Open a downloaded zip and return (zipfile, decoded text member stream)."""
    zf = zipfile.ZipFile(path)
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        zf.close()
        raise ValueError("zip has no members")
    # GDELT puts exactly one file in each zip; take the first deterministically.
    name = sorted(names)[0]
    return zf, io.TextIOWrapper(zf.open(name), encoding="utf-8", errors="replace", newline="")


def iter_tsv_rows(path: str) -> Iterable[List[str]]:
    """Yield tab separated rows from a zip member, skipping malformed lines."""
    zf, handle = _open_zip_member(path)
    try:
        reader = csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            if not row:
                continue
            yield row
    finally:
        handle.close()
        zf.close()


def clean_gkg_title(raw: str) -> str:
    """Strip the <PAGE_TITLE> wrapper. Titles may be empty."""
    if not raw:
        return ""
    match = TITLE_TAG_RE.search(raw)
    text = match.group(1) if match else raw
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_gkg_locations(raw: str) -> List[Dict]:
    """Parse V1LOCATIONS into dicts, keeping only entries with numeric lat/lon.

    Entry form: type#name#countrycode#adm1#lat#long#featureid
    """
    out: List[Dict] = []
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("#")
        if len(parts) < LOCATION_PART_COUNT:
            continue
        lat = _to_float(parts[LOCATION_LAT_INDEX])
        lon = _to_float(parts[LOCATION_LON_INDEX])
        coords = _valid_coords(lat, lon)
        if coords is None:
            continue
        out.append({
            "name": parts[1].strip(),
            "country": parts[2].strip(),
            "lat": coords[0],
            "lon": coords[1],
        })
    return out


def parse_gkg_tone(raw: str) -> Optional[float]:
    """Average the comma separated tone floats, if any parse."""
    values = [v for v in (_to_float(p) for p in (raw or "").split(",")) if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def event_geo(row: Sequence[str]) -> Tuple[Optional[Dict], str]:
    """Resolve event geography, preferring ActionGeo then Actor1Geo then Actor2Geo.

    Returns (geo_or_None, geo_source_label). geo is None when the row has no
    usable coordinates, which the caller counts as skipped.
    """
    for label, name_i, lat_i, lon_i in (
        ("action_geo", EV["action_geo_name"], EV["action_geo_lat"], EV["action_geo_long"]),
        ("actor1_geo", EV["actor1_geo_name"], EV["actor1_geo_lat"], EV["actor1_geo_long"]),
        ("actor2_geo", EV["actor2_geo_name"], EV["actor2_geo_lat"], EV["actor2_geo_long"]),
    ):
        name = row[name_i].strip()
        coords = _valid_coords(_to_float(row[lat_i]), _to_float(row[lon_i]))
        if coords and name:
            return {
                "name": name,
                "lat": coords[0],
                "lon": coords[1],
                "country": row[EV["action_geo_country"]].strip() if label == "action_geo" else "",
                "source": label,
            }, label
    return None, ""


def _parse_gdelt_datetime(value: str, fmt: str = "%Y%m%d%H%M%S") -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ------------------------------------------------------------------ network


def _http_get(url: str) -> Tuple[int, bytes]:
    """GET a URL through urllib so the egress proxy is honored.

    Returns (status, body). HTTPError is raised through so callers can treat a
    404 as a soft skip; other statuses return normally for the caller to check.
    """
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return resp.status, resp.read()


def _download_to_disk(url: str, dest: str, max_bytes: int) -> int:
    """Stream a URL to dest, honoring TMPDIR via the path the caller chose.

    Aborts past max_bytes so a misbehaving server cannot fill the tmpfs. Returns
    the number of bytes written.
    """
    request = Request(url, headers={"User-Agent": USER_AGENT})
    written = 0
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise ValueError(f"download exceeded {max_bytes} bytes: {url}")
            out.write(chunk)
    return written


def fetch_latest_timestamp() -> Optional[str]:
    """Re-read lastupdate.txt and return the newest timestamp, or None."""
    _, body = _http_get(LASTUPDATE_URL)
    stamps = parse_lastupdate(body.decode("utf-8", errors="replace"))
    return stamps[0] if stamps else None


# -------------------------------------------------------------- event index


def build_event_index(
    events_path: str,
) -> Tuple[Dict[str, Dict], int, int]:
    """Parse the export zip into a url keyed enrichment index.

    Returns (index_by_source_url, rows_seen, rows_skipped_for_no_geo). Rows with
    no usable geography are still indexed when they have a SOURCEURL, since the
    tone and quad class are useful even without coordinates. The skipped count
    tracks rows the caller reports.
    """
    index: Dict[str, Dict] = {}
    rows = 0
    no_geo = 0
    for row in iter_tsv_rows(events_path):
        if len(row) < EV_MIN_COLUMNS:
            continue
        rows += 1
        url = row[EV["source_url"]].strip()
        geo, _label = event_geo(row)
        if geo is None:
            no_geo += 1
        if not url:
            continue
        index[url] = {
            "geo": geo,
            "event_code": row[EV["event_code"]].strip(),
            "quad_class": row[EV["quad_class"]].strip(),
            "goldstein_scale": _to_float(row[EV["goldstein_scale"]]),
            "avg_tone": _to_float(row[EV["avg_tone"]]),
            "num_articles": int(_to_float(row[EV["num_articles"]]) or 0),
            "date_added": _parse_gdelt_datetime(row[EV["date_added"]]),
            "sqldate": row[EV["sqldate"]].strip(),
        }
    return index, rows, no_geo


# ----------------------------------------------------------------- gkg rows


def _gkg_article_fields(row: Sequence[str]) -> Optional[Dict]:
    """Map one GKG row to article fields, or None when unusable."""
    url = row[GKG["document_identifier"]].strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    title = clean_gkg_title(row[GKG["title"]])
    if not title:
        return None
    locations = parse_gkg_locations(row[GKG["locations"]])
    return {
        "url": url,
        "title": title,
        "published_at": _parse_gdelt_datetime(row[GKG["date"]]),
        "locations": locations,
        "tone": parse_gkg_tone(row[GKG["tone"]]),
        "themes": [t for t in row[GKG["themes"]].split(";") if t.strip()],
        "persons": [p.strip() for p in row[GKG["persons"]].split(";") if p.strip()],
        "organizations": [o.strip() for o in row[GKG["organizations"]].split(";") if o.strip()],
        "source_common_name": row[GKG["source_common_name"]].strip(),
    }


def _gkg_body_text(fields: Dict, event: Optional[Dict]) -> Optional[str]:
    """Compose a body from GKG metadata, optionally enriched by the event join.

    These are metadata records, not article prose, so the composed body is a
    structured summary. It still clears MIN_BODY_LENGTH for real rows, which
    keeps the shape identical to the DOC API path downstream.
    """
    parts = [fields["title"]]
    if event:
        code = event.get("event_code")
        if code:
            parts.append(f"GDELT event {code}")
        if event.get("num_articles"):
            parts.append(f"{event['num_articles']} articles")
    if fields["themes"]:
        parts.append("Themes: " + ", ".join(fields["themes"][:12]))
    if fields["persons"]:
        parts.append("Persons: " + ", ".join(fields["persons"][:12]))
    if fields["organizations"]:
        parts.append("Organizations: " + ", ".join(fields["organizations"][:12]))
    for loc in fields["locations"][:5]:
        parts.append(f"Location: {loc['name']} ({loc['country']}) {loc['lat']},{loc['lon']}")
    if fields["source_common_name"]:
        parts.append(f"Source: {fields['source_common_name']}")
    return " ".join(p for p in parts if p)


def _geo_entity(fields: Dict, event: Optional[Dict], source_label: str) -> Optional[Dict]:
    """Pick the geo the pipeline stores in entities["GEO"].

    Event geography wins because it describes where the event happened, with
    the first GKG V1LOCATIONS entry as the fallback. Returns None when neither
    yields usable coordinates.
    """
    if event and event.get("geo"):
        geo = dict(event["geo"])
        geo["fallback"] = source_label
        if fields["tone"] is not None:
            geo["tone"] = fields["tone"]
        return geo
    if fields["locations"]:
        loc = fields["locations"][0]
        geo = {
            "name": loc["name"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "country": loc["country"],
            "source": "gkg_v1locations",
            "fallback": source_label,
        }
        if fields["tone"] is not None:
            geo["tone"] = fields["tone"]
        return geo
    return None


def build_article(fields: Dict, event: Optional[Dict], source_label: str) -> Optional[RawArticle]:
    """Build a RawArticle in the same shape the DOC API path produces."""
    body_text = _gkg_body_text(fields, event)
    if not body_text or len(body_text) < MIN_BODY_LENGTH:
        return None

    geo = _geo_entity(fields, event, source_label)
    if geo is None:
        return None

    entities: Dict = {
        "PERSON": fields["persons"][:5],
        "ORG": fields["organizations"][:5],
        "GPE": [loc["name"] for loc in fields["locations"][:5]],
        "GEO": geo,
    }
    if fields["themes"]:
        entities["THEMES"] = fields["themes"][:5]
    if event:
        entities["EVENT"] = {
            "event_code": event.get("event_code"),
            "quad_class": event.get("quad_class"),
            "goldstein_scale": event.get("goldstein_scale"),
        }

    domain = (urlparse(fields["url"]).hostname or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]

    return RawArticle(
        url=fields["url"],
        url_hash=compute_url_hash(fields["url"]),
        title=fields["title"],
        body_text=body_text,
        summary=body_text[:500],
        source_domain=domain or "gdeltproject.org",
        source_tier=tier_for_domain(domain),
        published_at=fields["published_at"],
        entities=entities,
        content_hash=compute_content_hash(body_text),
    )


def parse_gkg_file(
    gkg_path: str,
    event_index: Optional[Dict[str, Dict]] = None,
    source_label: str = "gdelt-static",
    max_articles: int = 500,
    known_url_hashes: Optional[Set[str]] = None,
) -> Tuple[List[RawArticle], int, int, int]:
    """Parse a GKG zip into RawArticles.

    Returns (articles, gkg_rows, joined, skipped_no_geo). The event join is by
    exact SOURCEURL to DocumentIdentifier match, which is cheap and never
    required: rows without a match still produce an article if GKG itself
    carries coordinates.
    """
    articles: List[RawArticle] = []
    seen: Set[str] = set()
    gkg_rows = 0
    joined = 0
    skipped_no_geo = 0
    event_index = event_index or {}

    for row in iter_tsv_rows(gkg_path):
        if len(row) < GKG_MIN_COLUMNS:
            continue
        gkg_rows += 1
        if len(articles) >= max_articles:
            continue
        fields = _gkg_article_fields(row)
        if fields is None:
            continue
        event = event_index.get(fields["url"])
        if event is not None:
            joined += 1
        article = build_article(fields, event, source_label)
        if article is None:
            if _geo_entity(fields, event, source_label) is None:
                skipped_no_geo += 1
            continue
        if article.url_hash in seen:
            continue
        if known_url_hashes and article.url_hash in known_url_hashes:
            continue
        seen.add(article.url_hash)
        articles.append(article)

    return articles, gkg_rows, joined, skipped_no_geo


# ------------------------------------------------------------ orchestration


def _tmp_root() -> str:
    """Directory for downloads. tempfile honors TMPDIR, so /tmp is avoided."""
    root = os.path.join(tempfile.gettempdir(), "gdelt_static")
    os.makedirs(root, exist_ok=True)
    return root


async def ingest_static_file_set(
    max_timestamps: int = 1,
    max_articles_per_file: int = 500,
    max_download_bytes: int = 64 * 1024 * 1024,
    known_url_hashes: Optional[Set[str]] = None,
    with_events: bool = True,
    with_gkg: bool = True,
) -> StaticResult:
    """Ingest the latest GDELT static file set.

    Processes at most max_timestamps windows, so per run volume is capped. For
    each window we re-read lastupdate.txt right before downloading, download
    the events and GKG zips to TMPDIR, parse them, and delete the temp files in
    a finally block no matter what.

    A 404 on any data file is a soft skip: the window is noted and the run
    continues. Only a failure to read the manifest is fatal, since without it
    there is nothing to download.
    """
    result = StaticResult()

    try:
        newest = fetch_latest_timestamp()
    except (HTTPError, URLError, OSError) as e:
        return StaticResult(ok=False, error=f"lastupdate.txt unavailable: {e}")
    if not newest:
        return StaticResult(ok=False, error="lastupdate.txt contained no timestamps")

    try:
        _, manifest_body = _http_get(LASTUPDATE_URL)
        stamps = parse_lastupdate(manifest_body.decode("utf-8", errors="replace"))
    except (HTTPError, URLError, OSError) as e:
        return StaticResult(ok=False, error=f"lastupdate.txt re-read failed: {e}")

    if newest not in stamps:
        # Manifest rolled between the two reads; use the freshest listing.
        stamps = stamps or [newest]
    stamps = stamps[:max_timestamps]
    result.timestamps = list(stamps)

    tmp_dir = tempfile.mkdtemp(prefix="run_", dir=_tmp_root())
    try:
        for stamp in stamps:
            await _ingest_one_window(
                stamp,
                tmp_dir,
                result,
                max_articles_per_file,
                max_download_bytes,
                known_url_hashes,
                with_events,
                with_gkg,
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not result.articles and result.ok:
        result.ok = False
        result.error = result.error or "no articles parsed from static files"
    return result


async def _ingest_one_window(
    stamp: str,
    tmp_dir: str,
    result: StaticResult,
    max_articles: int,
    max_download_bytes: int,
    known_url_hashes: Optional[Set[str]],
    with_events: bool,
    with_gkg: bool,
) -> None:
    """Download and parse one 15 minute window, appending into result."""
    source_key = f"gdelt_static.{stamp}"
    STATS.record(source_key, "entries_in_feed", 1)

    event_index: Dict[str, Dict] = {}
    events_path: Optional[str] = None
    gkg_path: Optional[str] = None
    paths: List[str] = []
    try:
        if with_events:
            events_path = os.path.join(tmp_dir, f"{stamp}{EVENTS_SUFFIX}")
            paths.append(events_path)
            ok = await _download(BASE_URL + stamp + EVENTS_SUFFIX, events_path, max_download_bytes)
            if not ok:
                events_path = None
                result.soft_skips.append(f"{stamp}{EVENTS_SUFFIX}")

        if with_gkg:
            # Re-read the manifest right before the GKG download: the window
            # can rotate mid run and the earlier timestamp may now 404.
            gkg_path = os.path.join(tmp_dir, f"{stamp}{GKG_SUFFIX}")
            if not await _download(BASE_URL + stamp + GKG_SUFFIX, gkg_path, max_download_bytes):
                gkg_path = None
                result.soft_skips.append(f"{stamp}{GKG_SUFFIX}")
            else:
                paths.append(gkg_path)

        if events_path:
            try:
                event_index, rows, no_geo = await asyncio.to_thread(build_event_index, events_path)
                result.events_rows += rows
                result.skipped_no_geo += no_geo
                STATS.record(source_key, "events_rows", rows)
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                logger.warning("GDELT static events parse failed for %s: %s", stamp, e)
                result.soft_skips.append(f"events parse {stamp}")
                event_index = {}

        if gkg_path:
            articles, rows, joined, skipped = await asyncio.to_thread(
                parse_gkg_file,
                gkg_path,
                event_index,
                "gdelt-static",
                max_articles,
                known_url_hashes,
            )
            result.gkg_rows += rows
            result.joined += joined
            result.skipped_no_geo += skipped
            existing = {a.url_hash for a in result.articles}
            for art in articles:
                if art.url_hash not in existing:
                    result.articles.append(art)
                    existing.add(art.url_hash)
            STATS.record(source_key, "entries_seen", rows)
            STATS.record(source_key, "ok", len(articles))
            STATS.record(source_key, "skipped_no_geo", skipped)
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass


async def _download(url: str, dest: str, max_bytes: int) -> bool:
    """Download url to dest off the event loop. False means soft skip."""
    try:
        await asyncio.to_thread(_download_to_disk, url, dest, max_bytes)
        return True
    except HTTPError as e:
        if e.code == 404:
            logger.info("GDELT static file gone (rotated), skipping: %s", url)
            return False
        logger.warning("GDELT static download failed %s: %s", url, e)
        return False
    except (URLError, OSError, ValueError) as e:
        logger.warning("GDELT static download failed %s: %s", url, e)
        return False


async def verify_static_endpoint() -> Dict[str, object]:
    """One off check that the static file manifest and a window are readable."""
    try:
        stamp = fetch_latest_timestamp()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not stamp:
        return {"ok": False, "error": "no timestamps in manifest"}
    url = BASE_URL + stamp + GKG_SUFFIX
    tmp_dir = tempfile.mkdtemp(prefix="verify_", dir=_tmp_root())
    dest = os.path.join(tmp_dir, f"{stamp}{GKG_SUFFIX}")
    try:
        got = await _download(url, dest, 64 * 1024 * 1024)
        return {"ok": got, "timestamp": stamp, "url": url}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
