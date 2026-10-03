"""The route table is a contract: this module pins it against the pre-split app.

curation_ui/main.py used to hold every route. The split into one router per
surface (curation, story_api, globe, map_api, public_pages) moved code around,
and the only thing that must not move is what the app serves: the same paths, the
same methods, and require_auth on exactly the routes that had it. A refactor
that silently drops a dependency, flips a public route to auth-gated, or renames
a path is a security change disguised as a cleanup, so the whole table is
compared, not a sample of it.

EXPECTED_ROUTE_TABLE below was dumped from the unsplit app at commit 2946cec by
running the extraction in this file over curation_ui.main there and printing the
result, minus /globe: the globe page was removed on Tyler's directive
(2026-10-02) and test_globe_removed_is_not_served pins that. It is a literal,
not a recomputation, so the assertion is independent of the code it checks.

Since then the table has grown deliberately, and this file is the record of
that:

  2026-10-02  /story/{story_id} added and six triage routes removed. Tyler
              directed on 2026-10-02 that the curation page lose approve,
              reject and edit ("remove the approve, reject, edit"), so
              /story/{story_id}/approve, /story/{story_id}/reject,
              /story/{story_id}/save, /story/{story_id}/edit, /posts and
              /post/{post_id}/mark-posted are gone rather than unlinked, and
              /story/{story_id} now serves the read-only detail view in the slot
              the edit route used to occupy. It stays require_auth: story
              internals are not public. With those routes gone nothing in the
              app requires CSRF any more, which test_no_route_requires_csrf
              below pins so a mutating route added later cannot skip it.

  2026-10-02  /api/cron/checkpoint, /api/cron/checkpoint/watchdog
              added with the v2 transparency signer (curation_ui/cron.py).
              Both are bearer-token routes, NOT require_auth and NOT CSRF:
              Vercel Cron sends a GET with an Authorization header and no page
              to have issued a token, so HTTP Basic and the curator session
              model do not apply. The token check and its throttling are tested
              in tests/test_checkpoint_cron.py, and
              test_cron_routes_are_not_basic_authed below pins the weaker-looking
              (False, False) pair here so a future refactor cannot quietly turn
              a token route into an open one, or an open one into a token route,
              without this file changing.

  2026-10-03  /api/globe/stats removed. Its last caller was the /globe page's
              Cesium script, deleted with the page in 73ffd28, so the route has
              had no consumer since; /api/globe/events and /api/globe/layers
              stay because map.js plots the first and GRAND_PLAN Phase 4 plans
              the second. A path that stops being listed is a silent deletion, so
              test_globe_stats_is_gone pins the ABSENCE here as well as the two
              survivors' presence.
"""

import curation_ui.main as main_module
from curation_ui.main import app

# (path, methods-without-HEAD, require_auth, require_csrf)
EXPECTED_ROUTE_TABLE = [
    ("/", ("GET",), True, False),
    ("/api/cron/checkpoint", ("GET",), False, False),
    ("/api/cron/checkpoint/watchdog", ("GET",), False, False),
    ("/api/globe/events", ("GET",), False, False),
    ("/api/globe/layers", ("GET",), False, False),
    ("/api/map/freshness", ("GET",), False, False),
    ("/api/map/replay", ("GET",), False, False),
    ("/api/map/stories", ("GET",), False, False),
    ("/api/stories/{story_id}/sources", ("GET",), True, False),
    ("/api/stories/{story_id}/viewpoints", ("GET",), True, False),
    ("/docs", ("GET",), False, False),
    ("/docs/oauth2-redirect", ("GET",), False, False),
    ("/healthz", ("GET",), False, False),
    ("/healthz/details", ("GET",), True, False),
    ("/map", ("GET",), False, False),
    ("/openapi.json", ("GET",), False, False),
    ("/proof/{article_id}", ("GET",), False, False),
    ("/redoc", ("GET",), False, False),
    ("/stories/{story_id}", ("GET",), False, False),
    ("/story/{story_id}", ("GET",), True, False),
]


def iter_routes(routes, seen=None):
    """Yield every APIRoute, flattening include_router wrappers.

    FastAPI >= 0.14x keeps an included router as a nested _IncludedRouter object
    on app.routes instead of splicing its routes into the parent, so iterating
    app.routes directly silently misses /healthz and /healthz/details. Anything
    carrying a .routes list (or an original_router with one) is descended into.
    """
    seen = set() if seen is None else seen
    for route in routes:
        if id(route) in seen:
            continue
        seen.add(id(route))
        nested = getattr(route, "routes", None)
        if not isinstance(nested, list):
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None) if original is not None else None
        if isinstance(nested, list):
            yield from iter_routes(nested, seen)
            continue
        yield route


def dependency_names(dependant):
    """Every dependency function name in a route's dependency tree, transitively."""
    names = set()
    for sub in getattr(dependant, "dependencies", []) or []:
        call = sub.call
        names.add(getattr(call, "__name__", None) or repr(call))
        names |= dependency_names(sub)
    return names


def route_table(application):
    """[(path, methods, require_auth, require_csrf)] for one application."""
    rows = set()
    for route in iter_routes(application.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        names = dependency_names(getattr(route, "dependant", None))
        rows.add((
            path,
            tuple(sorted(m for m in methods if m != "HEAD")),
            "require_auth" in names,
            "require_csrf" in names,
        ))
    return sorted(rows)


class TestRouteTableMatchesPreSplitApp:
    def test_extraction_finds_the_whole_table(self):
        """Guard the guard: a walker that stops early must not pass the compare."""
        assert len(route_table(app)) == len(EXPECTED_ROUTE_TABLE)

    def test_route_table_is_identical_to_pre_split(self):
        assert route_table(app) == EXPECTED_ROUTE_TABLE

    def test_no_duplicate_paths(self):
        paths = [row[0] for row in route_table(app)]
        assert len(paths) == len(set(paths))

    def test_expected_table_lists_the_health_routes(self):
        """/healthz is reached through the health router; prove the walker sees it."""
        paths = {row[0] for row in route_table(app)}
        assert "/healthz" in paths
        assert "/healthz/details" in paths

    def test_expected_table_lists_every_surface(self):
        """One route from each module the split created, so a missing router fails."""
        by_path = {row[0]: row for row in route_table(app)}
        assert by_path["/"][2] is True
        assert by_path["/story/{story_id}"][2] is True
        assert by_path["/api/stories/{story_id}/viewpoints"][2] is True
        assert by_path["/api/stories/{story_id}/sources"][2] is True
        assert by_path["/api/globe/events"][2] is False
        assert by_path["/api/globe/layers"][2] is False
        assert by_path["/api/map/freshness"][2] is False
        assert by_path["/api/map/stories"][2] is False
        assert by_path["/api/map/replay"][2] is False
        assert by_path["/stories/{story_id}"][2] is False
        assert by_path["/proof/{article_id}"][2] is False
        assert by_path["/map"][2] is False
        assert by_path["/healthz"][2] is False
        assert by_path["/healthz/details"][2] is True

    def test_cron_routes_are_not_basic_authed(self):
        """The cron routes carry a bearer token, not require_auth -- pin it.

        They are deliberately (False, False): Vercel Cron cannot send Basic
        credentials or a CSRF token. What protects them is the bearer check in
        curation_ui/cron.py, which refuses when no token is configured at all.
        This assertion exists so that a later "let's just use require_auth here
        too" or "let's drop the token check" shows up as a route-table change.
        """
        by_path = {row[0]: row for row in route_table(app)}
        for path in ("/api/cron/checkpoint", "/api/cron/checkpoint/watchdog"):
            assert by_path[path][2] is False, path
            assert by_path[path][3] is False, path
            assert by_path[path][1] == ("GET",), "Vercel Cron can only issue GET"

    def test_the_triage_flows_are_gone_not_just_unlinked(self):
        """Tyler's directive was to remove approve, reject and edit.

        Hiding the buttons would have left the htmx endpoints live at a guessable
        URL and made the directive false, so the routes are deleted and this pins
        that. If a future batch reintroduces any of them, this fails on purpose.
        """
        paths = {row[0] for row in route_table(app)}
        for path in (
            "/story/{story_id}/approve",
            "/story/{story_id}/reject",
            "/story/{story_id}/save",
            "/story/{story_id}/edit",
            "/posts",
            "/post/{post_id}/mark-posted",
        ):
            assert path not in paths, path

    def test_no_route_requires_csrf(self):
        """The last CSRF-protected routes were the four triage mutations.

        With them gone the app has no state-changing route at all. Pinning the
        empty set means the next batch that adds one has to bring require_csrf
        with it and update this file, rather than shipping a POST that any
        cross-site form can fire.
        """
        guarded = {row[0] for row in route_table(app) if row[3]}
        assert guarded == set()

    def test_app_is_the_one_main_exports(self):
        """api/index.py and the test suite both import curation_ui.main:app."""
        assert main_module.app is app


class TestGlobeRemoved:
    """The globe page is gone; its data APIs stay.

    Tyler removed the page on 2026-10-02. /api/globe/events is what map.js plots,
    so the JSON survives under its existing paths while the HTML route is a 404.
    """

    def test_globe_page_is_not_served(self):
        """No route claims /globe, so an anonymous GET is a 404, not a 200."""
        from fastapi.testclient import TestClient

        assert TestClient(app).get("/globe").status_code == 404

    def test_globe_json_apis_are_still_served(self):
        """map.js plots /api/globe/events, so removing the page keeps the JSON."""
        paths = {row[0] for row in route_table(app)}
        assert "/api/globe/events" in paths
        assert "/api/globe/layers" in paths

    def test_globe_stats_is_gone(self):
        """/api/globe/stats is deleted, not merely unlisted.

        An assertion that stops mentioning a path passes whether the route was
        removed or left behind, so the deletion is pinned as an absence at both
        levels: no route claims the path, and an anonymous GET is a 404 rather
        than a 500 (a 500 would mean something still tries to serve it).
        """
        from fastapi.testclient import TestClient

        assert "/api/globe/stats" not in {row[0] for row in route_table(app)}
        assert "/api/globe/stats" not in app.openapi()["paths"]

        response = TestClient(app).get("/api/globe/stats")
        assert response.status_code == 404

    def test_no_surviving_route_renders_the_globe_template(self):
        """globe.html is deleted; a route reaching for it would 500 at request time."""
        assert "/globe" not in {row[0] for row in route_table(app)}

    def test_no_template_or_static_asset_still_loads_globe_scripts(self):
        """The globe assets are deleted, so no page may reference them."""
        import os
        import re

        import curation_ui.app_state as app_state_module

        base = os.path.dirname(os.path.abspath(app_state_module.__file__))
        pattern = re.compile(r"/static/globe/|[\"']globe\.html[\"']|[\"']/globe[\"']")
        offenders = []
        for subdir in ("templates", "static"):
            root = os.path.join(base, subdir)
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if not name.endswith((".html", ".js", ".css")):
                        continue
                    full = os.path.join(dirpath, name)
                    with open(full, encoding="utf-8") as handle:
                        text = handle.read()
                    if pattern.search(text):
                        offenders.append(os.path.relpath(full, base))
        assert offenders == []