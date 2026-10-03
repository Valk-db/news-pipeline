"""Edge caching for the public map reads.

The map's read APIs return the same bytes to every visitor between ingests, so
without this header Vercel runs one function invocation per visitor per pan. With
it, the edge serves the body and the function is not invoked at all.

What makes this risky is not the header, it is the allowlist. A public
Cache-Control on a response that turns out to be user-specific, or on an
authenticated route, hands one visitor another visitor's data at every CDN node
on the planet, and it keeps handing it back for the whole TTL. So:

  * the allowlist is positive and closed. Anything not named here is never
    cached, which means a route added later is uncached by default rather than
    public by default. The danger of a leak is a missing entry; the danger of a
    miss is one extra invocation.
  * nothing that varies by caller is on it. The six paths below take only query
    parameters that are part of the public URL, so one URL is one body.
  * an error body is never cached. When the database is unreachable these
    handlers return HTTP 200 with {"error": ...} rather than a 5xx, so a status
    check alone would happily let a database outage into the cache for the whole
    TTL. The body is inspected for that case, which is why this reads the
    response instead of only reading the request.
  * a response that sets a cookie is never cached, defensively, so a future
    session or CSRF cookie on one of these paths cannot be pinned into a shared
    store.

The TTL is derived from the ingest cadence rather than picked. .github/workflows/
daily-ingest.yml runs '23 6,18 * * *', so fresh data lands every 12 hours. See
MAP_READ_S_MAXAGE for why the TTL is 30 minutes and not 12.
"""

import json
import logging

from fastapi import Request

logger = logging.getLogger(__name__)

# The map read APIs, and nothing else. /map and /stories/{id} are deliberately
# absent: they are HTML pages, not the read APIs this backlog item is about, and
# /map is revalidated per visitor anyway.
MAP_READ_PATHS = frozenset(
    {
        "/api/globe/events",
        "/api/globe/layers",
        "/api/map/freshness",
        "/api/map/replay",
        "/api/map/stories",
    }
)

# s-maxage is how long the edge may serve this body without asking again.
#
# The ingest runs every 12 hours, so a TTL equal to the ingest interval would
# serve data up to 24 hours old: a body cached at 06:30 is still being served at
# 18:00, an hour before the refresh that would replace it. 30 minutes keeps the
# worst case at "one ingest late", which is what a visitor sees anyway between
# 06:23 and 18:23, while still absorbing the burst of refetches a pan or a zoom
# produces -- the same body for a hundred pan positions in a row.
MAP_READ_S_MAXAGE = 1800

# stale-while-revalidate: past the TTL the edge serves the last good body and
# revalidates in the background. An hour covers the fan-out right after an
# ingest lands, where every edge going cold at once would otherwise be a burst
# of invocations against a database that is itself mid-write.
MAP_READ_STALE_WHILE_REVALIDATE = 3600


def map_read_cache_control() -> str:
    """The exact header value the map reads are served with."""
    return (
        f"public, s-maxage={MAP_READ_S_MAXAGE}, "
        f"stale-while-revalidate={MAP_READ_STALE_WHILE_REVALIDATE}"
    )


def is_cacheable_map_read(path: str, method: str) -> bool:
    """Whether this request is a map read the edge may serve from cache.

    Positive allowlist, GET only. Writes never match, and a HEAD is not matched
    because none of these routes declare one: letting an unmatched method through
    on the strength of a path match would be the kind of near-miss that turns
    into a cache poisoning bug the first time a route changes.
    """
    return method == "GET" and path in MAP_READ_PATHS


def _body_is_error(body: bytes) -> bool:
    """True when the payload is the {"error": ...} shape the handlers return when
    the database is unreachable.

    Unparseable bodies are not errors: a non-JSON body from a future change should
    not silently become uncacheable forever, and none of these routes serve one.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("error"))


async def _replay(body: bytes):
    """Re-iterable one-chunk body, so a consumed response can still be sent."""
    yield body


async def map_read_cache(request: Request, call_next):
    """Attach the map read Cache-Control, and only where it is safe to.

    Registered in main.py the same way as the CSP middleware, so it stays a plain
    two-argument function that the tests can drive with a fake call_next instead
    of booting the app.
    """
    response = await call_next(request)
    if not is_cacheable_map_read(request.url.path, request.method):
        return response
    if response.status_code != 200:
        return response
    if response.headers.get("set-cookie"):
        # Defensive: none of these routes set one today.
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    if _body_is_error(body):
        # Restore and return without a Cache-Control: a database outage must not
        # be pinned into the edge for the next half hour.
        response.body_iterator = _replay(body)
        response.headers["content-length"] = str(len(body))
        return response

    response.body_iterator = _replay(body)
    response.headers["content-length"] = str(len(body))
    response.headers["Cache-Control"] = map_read_cache_control()
    return response


__all__ = [
    "MAP_READ_PATHS",
    "MAP_READ_S_MAXAGE",
    "MAP_READ_STALE_WHILE_REVALIDATE",
    "map_read_cache_control",
    "is_cacheable_map_read",
    "map_read_cache",
]