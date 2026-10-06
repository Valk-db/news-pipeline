"""The map read Cache-Control is a security control, so it is tested as one.

The header itself is unremarkable. The failure mode is a route that ends up with
a public, shared-cacheable response it should not have: an authenticated route, a
write, or anything whose body depends on who is asking. At a CDN that mistake is
not a bug in one request, it is one visitor's data served to every other visitor
from every edge node for the length of the TTL, and the unit test that would have
caught it is the one this file exists to be.

The allowlist is therefore checked against the real, pinned route table rather
than against a list written here, so a route that gains require_auth cannot be
sitting in MAP_READ_PATHS by accident.
"""

import json
import uuid

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import curation_ui.main  # noqa: F401  (registers middleware on the real app)
from curation_ui.cache import (
    MAP_READ_PATHS,
    MAP_READ_S_MAXAGE,
    MAP_READ_STALE_WHILE_REVALIDATE,
    is_cacheable_map_read,
    map_read_cache,
    map_read_cache_control,
)
from tests.test_route_table import EXPECTED_ROUTE_TABLE, route_table
from curation_ui.main import app


async def _body_response(body: bytes, status_code: int = 200, headers=None):
    """A response shaped like the ones the handlers actually produce.

    The middleware reads response.body_iterator, so a test that used a plain
    Response would not exercise the code path the app runs on: Starlette hands
    the middleware a StreamingResponse, not a buffered one.
    """

    async def iterator():
        yield body

    return StreamingResponse(iterator(), status_code=status_code, headers=headers or {})


async def _call(request: Request):
    body = json.dumps({"features": []}).encode()
    return await _body_response(body)


def _request(path: str, method: str = "GET") -> Request:
    return Request({
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
        "root_path": "",
    })


async def _collected(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


async def _ok(request):
    return await _body_response(b'{"ok":true}')


async def _boom(request):
    return await _body_response(b'{"error":"down"}', status_code=200)


class TestAllowlistAgainstTheRealRouteTable:
    def test_allowlist_paths_all_exist_in_the_app(self):
        """A path in the allowlist that no longer exists is dead weight, and a
        path that exists only under a prefix change is a near miss waiting to
        become a real one."""
        real = {row[0] for row in route_table(app)}
        assert real >= MAP_READ_PATHS

    def test_allowlist_never_names_an_authenticated_route(self):
        """The bug this file exists for: a public Cache-Control on a route that
        require_auth protects."""
        authenticated = {path for path, _m, auth, _c in EXPECTED_ROUTE_TABLE if auth}
        assert not (MAP_READ_PATHS & authenticated)

    def test_allowlist_is_exactly_the_map_reads(self):
        """Pinned as a literal so adding an entry is a deliberate act that shows
        up in the diff, rather than something a refactor can do sideways."""
        assert frozenset({
            "/api/globe/events",
            "/api/globe/layers",
            "/api/map/freshness",
            "/api/map/replay",
            "/api/map/stories",
        }) == MAP_READ_PATHS

    def test_every_allowlisted_route_is_get_only(self):
        """A write on a cached path would be a correctness bug before it was a
        security one."""
        methods = {path: m for path, m, _a, _c in EXPECTED_ROUTE_TABLE}
        for path in MAP_READ_PATHS:
            assert set(methods[path]) == {"GET"}, path

    def test_html_pages_are_not_allowlisted(self):
        """The pages are the app's own surface, not the read APIs. /map in
        particular is refetched per visitor, and caching an HTML page would be a
        different decision than this batch made."""
        assert "/map" not in MAP_READ_PATHS
        assert "/stories/{story_id}" not in MAP_READ_PATHS
        assert "/proof/{article_id}" not in MAP_READ_PATHS


class TestIsCacheableMapRead:
    def test_allowlisted_get_is_cacheable(self):
        assert is_cacheable_map_read("/api/globe/events", "GET")

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def test_other_methods_are_not(self, method):
        assert not is_cacheable_map_read("/api/globe/events", method)

    def test_unknown_path_is_not(self):
        assert not is_cacheable_map_read("/api/secret", "GET")

    def test_authenticated_paths_are_not(self):
        for path in ("/story/{story_id}", "/healthz/details", "/api/stories/{story_id}/sources"):
            assert not is_cacheable_map_read(path, "GET")


class TestHeaderValue:
    def test_exact_value(self):
        assert map_read_cache_control() == (
            "public, s-maxage=1800, stale-while-revalidate=3600"
        )

    def test_ttl_is_derived_from_the_ingest_interval(self):
        """The cron is every 12 hours. A TTL at or above that interval serves
        data up to 24 hours old; the point of the 30 minutes is that the cache
        never outlives the gap between ingests."""
        assert MAP_READ_S_MAXAGE < 12 * 3600
        assert MAP_READ_STALE_WHILE_REVALIDATE <= 12 * 3600
        assert MAP_READ_STALE_WHILE_REVALIDATE > MAP_READ_S_MAXAGE


class TestMiddlewareBehaviour:
    async def test_allowlisted_get_gets_the_header_and_keeps_its_body(self):
        response = await map_read_cache(_request("/api/globe/events"), _call)
        assert response.headers["Cache-Control"] == map_read_cache_control()
        assert await _collected(response) == json.dumps({"features": []}).encode()

    async def test_error_body_is_not_cached(self):
        """The dangerous one. These handlers answer a database outage with HTTP
        200 and {"error": ...}, so a status check alone would cache the outage
        for the whole TTL."""

        async def call_next(request):
            return await _body_response(
                json.dumps({"error": "Database temporarily unavailable"}).encode()
            )

        response = await map_read_cache(_request("/api/globe/events"), call_next)
        assert "Cache-Control" not in response.headers
        assert b"error" in await _collected(response)

    async def test_empty_result_is_still_cached(self):
        """The other direction. An empty list is the normal answer between
        ingests, and treating it as uncacheable would defeat the whole header."""

        async def call_next(request):
            return await _body_response(json.dumps([]).encode())

        response = await map_read_cache(_request("/api/globe/events"), call_next)
        assert response.headers["Cache-Control"] == map_read_cache_control()

    async def test_empty_error_string_is_cached(self):
        """Falsy error, not a key. Gating on presence rather than truth would
        drop the cache on any response that happens to carry the key."""

        async def call_next(request):
            return await _body_response(json.dumps({"error": "", "ok": True}).encode())

        response = await map_read_cache(_request("/api/globe/events"), call_next)
        assert response.headers["Cache-Control"] == map_read_cache_control()

    async def test_non_json_body_is_cached(self):
        async def call_next(request):
            return await _body_response(b"not json at all")

        response = await map_read_cache(_request("/api/globe/events"), call_next)
        assert response.headers["Cache-Control"] == map_read_cache_control()

    async def test_unlisted_path_passes_through_untouched(self):
        response = await map_read_cache(_request("/stories/abc"), _call)
        assert "Cache-Control" not in response.headers
        assert await _collected(response) == json.dumps({"features": []}).encode()

    async def test_post_to_an_allowlisted_path_passes_through(self):
        response = await map_read_cache(_request("/api/globe/events", "POST"), _call)
        assert "Cache-Control" not in response.headers

    async def test_non_200_passes_through(self):
        async def call_next(request):
            return await _body_response(b"nope", status_code=503)

        response = await map_read_cache(_request("/api/globe/events"), call_next)
        assert "Cache-Control" not in response.headers

    async def test_set_cookie_response_is_never_cached(self):
        """Defensive. None of these routes set one today; if a future change
        adds a session cookie to one, this stops it being pinned into a shared
        store for every visitor."""

        async def call_next(request):
            return await _body_response(
                json.dumps({"features": []}).encode(),
                headers={"set-cookie": "session=abc; Path=/"},
            )

        response = await map_read_cache(_request("/api/globe/events"), call_next)
        assert "Cache-Control" not in response.headers

    async def test_content_length_matches_the_replayed_body(self):
        """The body is consumed to inspect it, so it has to be handed back with
        a length that matches or the client hangs on a truncated read."""
        response = await map_read_cache(_request("/api/globe/events"), _call)
        body = await _collected(response)
        assert response.headers["content-length"] == str(len(body))


class TestEndToEndThroughTheRealApp:
    """The middleware is registered on the real app, so prove it actually runs
    there. These run with no database, which is itself the interesting case: the
    real handlers answer a down database with HTTP 200 and an error body, and
    the middleware has to decline to cache it."""

    @pytest.fixture
    def client(self, monkeypatch):
        """Pin the database to unavailable.

        Left to itself this class inherits whatever the rest of the suite has
        done to settings: run alone the app has no DATABASE_URL and the map
        reads answer 200 with an error body, but another test setting that
        variable mid-run makes the same reads fall through check_database into
        get_session and raise instead. Neither is this middleware's behaviour,
        and a test that only passes in one ordering is not a test.
        """
        monkeypatch.setattr(app.state, "check_database_available", lambda: (False, "down"))
        return TestClient(app)

    def test_database_outage_is_not_cached(self, client):
        """The real app, no mocking. /api/globe/events returns 200 with
        {"error": ...} here, which is exactly the shape that must not reach a
        shared cache."""
        response = client.get("/api/globe/events")
        assert response.status_code == 200
        assert response.json()["error"]
        assert "Cache-Control" not in response.headers

    def test_every_map_read_declines_to_cache_while_the_database_is_down(self, client):
        for path in sorted(MAP_READ_PATHS):
            assert "Cache-Control" not in client.get(path).headers, path

    def test_healthz_is_not_cached(self, client):
        assert "Cache-Control" not in client.get("/healthz").headers

    def test_map_page_is_not_cached(self, client):
        """503 here because there is no database; the assertion is that nothing
        public is attached either way."""
        response = client.get("/map")
        assert "public" not in response.headers.get("Cache-Control", "")

    def test_authenticated_route_sends_no_public_cache_control(self, client):
        """401 is the unauthenticated answer here, which is the safe one; the
        assertion that matters is that nothing public is attached to it."""
        response = client.get("/story/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 401
        assert "public" not in response.headers.get("Cache-Control", "")

    def test_real_layers_payload_is_cached(self, monkeypatch):
        """The positive case through the real route, the real handler and the
        real response, with only the database replaced. Without this the class
        above would pass even if the middleware never attached a header at all,
        which is the shape a broken registration takes.

        /api/globe/stats used to be the path here and was removed on 2026-10-03,
        so the positive case moved to a route that still exists rather than
        going away with it.
        """
        import contextlib

        import curation_ui.globe as globe_module

        class _Layer:
            """The handler reads these attributes by name off the ORM row."""

            def __init__(self):
                self.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
                self.name = "conflict"
                self.description = "Armed conflicts"
                self.filter_criteria = {"event_type": ["conflict"]}
                self.style = {"color": "#e74c3c"}
                self.is_default = True
                self.is_visible = True
                self.min_zoom = 1.0
                self.max_zoom = 10.0
                self.color = "#e74c3c"

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        class _Session:
            async def execute(self, _statement):
                return _Result([_Layer()])

        @contextlib.asynccontextmanager
        async def fake_session():
            yield _Session()

        monkeypatch.setattr(globe_module, "check_database_public", lambda request: (True, ""))
        monkeypatch.setattr(globe_module, "get_session", fake_session)

        response = TestClient(app).get("/api/globe/layers")
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        assert payload["layers"][0]["name"] == "conflict"
        assert response.headers["Cache-Control"] == map_read_cache_control()
        assert "Content-Security-Policy" in response.headers

    def test_the_removed_globe_stats_path_is_not_cached(self, client):
        """A deleted route must not linger in the allowlist.

        /api/globe/stats was removed on 2026-10-03. Its 404 is not cacheable
        anyway, but a stale entry would be a path in MAP_READ_PATHS that no
        longer resolves, which is exactly the drift
        test_allowlist_paths_all_exist_in_the_app exists to catch -- pinned here
        from the other direction so the deletion cannot be half-reverted.
        """
        assert "/api/globe/stats" not in MAP_READ_PATHS
        response = client.get("/api/globe/stats")
        assert response.status_code == 404
        assert "Cache-Control" not in response.headers


class TestOnABareApp:
    """A mini app with the middleware alone, so the assertions are about the
    middleware and not about whatever the real handlers happen to return."""

    def build(self):
        application = Starlette(routes=[
            Route("/api/globe/events", _ok, methods=["GET"]),
            Route("/api/map/replay", _boom, methods=["GET"]),
        ])
        application.add_middleware(BaseHTTPMiddleware, dispatch=map_read_cache)
        return TestClient(application)

    def test_ok_route_is_cached(self):
        response = self.build().get("/api/globe/events")
        assert response.headers["Cache-Control"] == map_read_cache_control()
        assert response.json() == {"ok": True}

    def test_error_route_is_not_cached_and_still_reports(self):
        response = self.build().get("/api/map/replay")
        assert "Cache-Control" not in response.headers
        assert response.json() == {"error": "down"}

    def test_unknown_route_404_is_not_cached(self):
        response = self.build().get("/api/nope")
        assert response.status_code == 404
        assert "Cache-Control" not in response.headers
