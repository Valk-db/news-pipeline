"""Geocoding service for entity and event location resolution."""

import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class GeoResult:
    """Geocoding result."""
    name: str
    latitude: float
    longitude: float
    location_type: str  # city, country, region, point
    country_code: Optional[str] = None
    admin1: Optional[str] = None  # State/province
    admin2: Optional[str] = None  # County/district
    geonames_id: Optional[str] = None
    raw: Optional[Dict] = None


class Geocoder:
    """
    Multi-provider geocoder with caching.

    Supports:
    - Nominatim (OpenStreetMap) - free, rate limited
    - GeoNames - free with account
    - Mapbox - paid, high quality
    - Google Maps - paid
    """

    def __init__(
        self,
        nominatim_url: str = "https://nominatim.openstreetmap.org",
        geonames_username: Optional[str] = None,
        mapbox_token: Optional[str] = None,
        google_api_key: Optional[str] = None,
        cache_ttl_hours: int = 168,  # 1 week
    ):
        self.nominatim_url = nominatim_url
        self.geonames_username = geonames_username
        self.mapbox_token = mapbox_token
        self.google_api_key = google_api_key
        self.cache_ttl_hours = cache_ttl_hours

        self._cache: Dict[str, Tuple[GeoResult, datetime]] = {}
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        await self._client.aclose()

    def _cache_key(self, query: str, provider: str) -> str:
        return f"{provider}:{query.lower().strip()}"

    def _get_cached(self, key: str) -> Optional[GeoResult]:
        if key in self._cache:
            result, cached_at = self._cache[key]
            if (datetime.now(timezone.utc) - cached_at).total_seconds() < self.cache_ttl_hours * 3600:
                return result
            else:
                del self._cache[key]
        return None

    def _set_cached(self, key: str, result: GeoResult):
        self._cache[key] = (result, datetime.now(timezone.utc))

    async def geocode_nominatim(self, query: str, limit: int = 1) -> List[GeoResult]:
        """Geocode using Nominatim (OpenStreetMap)."""
        key = self._cache_key(query, "nominatim")
        cached = self._get_cached(key)
        if cached:
            return [cached]

        try:
            params = {
                "q": query,
                "format": "json",
                "limit": limit,
                "addressdetails": 1,
                "extratags": 1,
            }
            headers = {"User-Agent": "news-pipeline/1.0 (https://github.com/Valk-db/news-pipeline; admin@valk-db.com)"}

            response = await self._client.get(
                f"{self.nominatim_url}/search",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data:
                result = GeoResult(
                    name=item.get("display_name", query),
                    latitude=float(item["lat"]),
                    longitude=float(item["lon"]),
                    location_type=item.get("type", "point"),
                    country_code=item.get("address", {}).get("country_code", "").upper(),
                    admin1=item.get("address", {}).get("state"),
                    admin2=item.get("address", {}).get("county"),
                    geonames_id=None,
                    raw=item,
                )
                results.append(result)

            if results:
                self._set_cached(key, results[0])

            return results

        except Exception as e:
            logger.warning(f"Nominatim geocoding failed for '{query}': {e}")
            return []

    async def geocode_geonames(self, query: str, max_rows: int = 10) -> List[GeoResult]:
        """Geocode using GeoNames."""
        if not self.geonames_username:
            return []

        key = self._cache_key(query, "geonames")
        cached = self._get_cached(key)
        if cached:
            return [cached]

        try:
            params = {
                "q": query,
                "maxRows": max_rows,
                "username": self.geonames_username,
                "type": "json",
            }

            response = await self._client.get(
                "http://api.geonames.org/searchJSON",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("geonames", []):
                result = GeoResult(
                    name=item.get("name", query),
                    latitude=float(item["lat"]),
                    longitude=float(item["lng"]),
                    location_type=item.get("fcode", "point"),
                    country_code=item.get("countryCode", "").upper(),
                    admin1=item.get("adminName1"),
                    admin2=item.get("adminName2"),
                    geonames_id=str(item.get("geonameId")),
                    raw=item,
                )
                results.append(result)

            if results:
                self._set_cached(key, results[0])

            return results

        except Exception as e:
            logger.warning(f"GeoNames geocoding failed for '{query}': {e}")
            return []

    async def geocode_mapbox(self, query: str, limit: int = 5) -> List[GeoResult]:
        """Geocode using Mapbox."""
        if not self.mapbox_token:
            return []

        key = self._cache_key(query, "mapbox")
        cached = self._get_cached(key)
        if cached:
            return [cached]

        try:
            params = {
                "access_token": self.mapbox_token,
                "limit": limit,
            }

            response = await self._client.get(
                f"https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("features", []):
                coords = item.get("center", [0, 0])
                result = GeoResult(
                    name=item.get("place_name", query),
                    latitude=coords[1],
                    longitude=coords[0],
                    location_type=item.get("place_type", ["point"])[0],
                    country_code=None,
                    admin1=None,
                    admin2=None,
                    geonames_id=None,
                    raw=item,
                )
                results.append(result)

            if results:
                self._set_cached(key, results[0])

            return results

        except Exception as e:
            logger.warning(f"Mapbox geocoding failed for '{query}': {e}")
            return []

    async def geocode(self, query: str, providers: List[str] = None) -> Optional[GeoResult]:
        """
        Geocode a query using multiple providers in order.

        Args:
            query: Location query string
            providers: List of providers to try in order (default: all available)

        Returns:
            Best GeoResult or None
        """
        if providers is None:
            providers = []
            if self.nominatim_url:
                providers.append("nominatim")
            if self.geonames_username:
                providers.append("geonames")
            if self.mapbox_token:
                providers.append("mapbox")

        for provider in providers:
            try:
                if provider == "nominatim":
                    results = await self.geocode_nominatim(query, limit=1)
                elif provider == "geonames":
                    results = await self.geocode_geonames(query, max_rows=1)
                elif provider == "mapbox":
                    results = await self.geocode_mapbox(query, limit=1)
                else:
                    continue

                if results:
                    logger.info(f"Geocoded '{query}' using {provider}")
                    return results[0]

            except Exception as e:
                logger.warning(f"Provider {provider} failed for '{query}': {e}")

        logger.warning(f"All geocoding providers failed for '{query}'")
        return None

    async def reverse_geocode(self, lat: float, lon: float) -> Optional[GeoResult]:
        """Reverse geocode coordinates to place name."""
        key = self._cache_key(f"{lat},{lon}", "reverse")
        cached = self._get_cached(key)
        if cached:
            return cached

        try:
            params = {
                "lat": lat,
                "lon": lon,
                "format": "json",
                "addressdetails": 1,
            }
            headers = {"User-Agent": "news-pipeline/1.0 (https://github.com/Valk-db/news-pipeline; admin@valk-db.com)"}

            response = await self._client.get(
                f"{self.nominatim_url}/reverse",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            item = response.json()

            result = GeoResult(
                name=item.get("display_name", f"{lat}, {lon}"),
                latitude=lat,
                longitude=lon,
                location_type=item.get("type", "point"),
                country_code=item.get("address", {}).get("country_code", "").upper(),
                admin1=item.get("address", {}).get("state"),
                admin2=item.get("address", {}).get("county"),
                geonames_id=None,
                raw=item,
            )

            self._set_cached(key, result)
            return result

        except Exception as e:
            logger.warning(f"Reverse geocoding failed for {lat},{lon}: {e}")
            return None


# Global geocoder instance
_geocoder: Optional[Geocoder] = None


def get_geocoder(
    nominatim_url: str = "https://nominatim.openstreetmap.org",
    geonames_username: Optional[str] = None,
    mapbox_token: Optional[str] = None,
) -> Geocoder:
    """Get or create global geocoder instance."""
    global _geocoder
    if _geocoder is None:
        _geocoder = Geocoder(
            nominatim_url=nominatim_url,
            geonames_username=geonames_username,
            mapbox_token=mapbox_token,
        )
    return _geocoder


# Convenience functions
async def geocode_entity(entity_name: str, entity_type: str) -> Optional[Tuple[float, float]]:
    """Geocode an entity name and return (lat, lon)."""
    # Add context based on entity type
    query = entity_name
    if entity_type == "GPE":
        query += " city"
    elif entity_type == "LOC":
        query += " location"

    geocoder = get_geocoder()
    result = await geocoder.geocode(query)
    if result:
        return (result.latitude, result.longitude)
    return None


async def batch_geocode_entities(entities: List[Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    """Geocode multiple entities in parallel."""

    async def geocode_one(entity):
        name = entity.get("name", "")
        etype = entity.get("type", "")
        coords = await geocode_entity(name, etype)
        return (entity.get("id", name), coords)

    tasks = [geocode_one(e) for e in entities]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    coords = {}
    for result in results:
        if isinstance(result, tuple) and result[1]:
            coords[result[0]] = result[1]

    return coords