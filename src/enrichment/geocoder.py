"""Free place geocoding for canonical geopolitical entities.

One provider: Nominatim (OpenStreetMap). Keyless, no account, no data-sharing
opt-in, and it answers for the country/state/city names that make up news
copy. Verified live on 2026-10-02 against ten real names drawn from the dev
corpus (Iran, Israel, Tel Aviv, Alabama, Minnesota, Tennessee, South Africa,
Manchester City, Europe, Balkans): ten out of ten resolved, and every
coordinate matched the place.

Deliberately not used, with evidence:
  - GeoNames needs a registered username, i.e. a new account.
  - Mapbox and Google need paid tokens. The previous version also accepted a
    ``google_api_key`` it never used and advertised support it did not have.

Transport is urllib in a worker thread rather than httpx. The egress proxy on
this box is advertised in the environment in a form httpx cannot parse, so an
httpx client dies at construction ("Invalid port"); urllib honors the proxy
variables. That is the same reason src/enrichment/translation.py reaches MyMemory
over urllib.

Nominatim's usage policy allows one request per second and wants an
identifying User-Agent, so requests are paced and repeat lookups are answered
from an in-process cache. A lookup that fails returns None and is remembered as
a failure for the length of the run: a geocoder that is down should cost one
request per name, not one per retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = (
    "news-pipeline/1.0 (https://github.com/Valk-db/news-pipeline; admin@valk-db.com)"
)
NOMINATIM_TIMEOUT_SECONDS = 20.0
NOMINATIM_POLITENESS_SECONDS = 1.0


@dataclass(frozen=True)
class GeoResult:
    """One geocoded place: the point Nominatim picked and how much to trust it.

    ``location_type`` is Nominatim's own granularity label ("city", "state",
    "country", "continent", ...), which is what lets a caller prefer the most
    specific of several geocoded names. ``importance`` is Nominatim's prominence
    score for the match, 0.0 to 1.0, where a country sits near 1.0 and a hamlet
    far below it. It is the only signal that separates a confident match from a
    lucky one: "Man City" resolves to a village in Cote d'Ivoire with a weak
    score, "Manchester City" to Manchester with a strong one, and nothing else
    about the two results says which one was meant.
    """

    name: str
    latitude: float
    longitude: float
    location_type: str
    importance: float = 0.0


def parse_nominatim_hit(hit: dict) -> Optional[GeoResult]:
    """One Nominatim hit as a GeoResult, or None when it carries no usable point."""
    try:
        latitude = float(hit["lat"])
        longitude = float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    try:
        importance = float(hit.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    return GeoResult(
        name=hit.get("display_name") or "",
        latitude=latitude,
        longitude=longitude,
        location_type=hit.get("addresstype") or hit.get("type") or "place",
        importance=importance,
    )


def _fetch_nominatim(name: str) -> Optional[GeoResult]:
    """Blocking single-place lookup. Runs in a worker thread, never on the loop."""
    params = urllib.parse.urlencode(
        {"q": name, "format": "json", "limit": 1, "addressdetails": 1}
    )
    request = urllib.request.Request(
        f"{NOMINATIM_SEARCH_URL}?{params}",
        headers={"User-Agent": NOMINATIM_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=NOMINATIM_TIMEOUT_SECONDS) as response:
        hits = json.loads(response.read().decode("utf-8"))
    for hit in hits or []:
        result = parse_nominatim_hit(hit)
        if result is not None:
            return result
    return None


class Geocoder:
    """Nominatim lookups, paced to one request per second and cached in-process."""

    def __init__(self) -> None:
        # A None value is a remembered failure, not a missing entry: see the
        # module docstring.
        self._cache: Dict[str, Optional[GeoResult]] = {}
        self._last_request = 0.0

    async def _pace(self) -> None:
        """Wait out whatever is left of the politeness interval."""
        wait = NOMINATIM_POLITENESS_SECONDS - (time.monotonic() - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)

    async def geocode(self, name: str) -> Optional[GeoResult]:
        """Best match for a place name, or None when the name does not resolve.

        Never raises: geocoding is enrichment, and an unreachable geocoder must
        not stop a story from being ingested.
        """
        key = name.strip().lower()
        if not key:
            return None
        if key not in self._cache:
            await self._pace()
            try:
                self._cache[key] = await asyncio.to_thread(_fetch_nominatim, name)
            except Exception as exc:
                logger.warning("Nominatim lookup failed for %r: %s", name, exc)
                self._cache[key] = None
            self._last_request = time.monotonic()
        return self._cache[key]


_geocoder = Geocoder()


def get_geocoder() -> Geocoder:
    """The process-wide geocoder, so its cache and its 1/s pacing are shared."""
    return _geocoder
