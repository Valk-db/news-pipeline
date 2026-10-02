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
"""

import curation_ui.main as main_module
from curation_ui.main import app

# (path, methods-without-HEAD, require_auth, require_csrf)
EXPECTED_ROUTE_TABLE = [
    ("/", ("GET",), True, False),
    ("/api/globe/events", ("GET",), False, False),
    ("/api/globe/layers", ("GET",), False, False),
    ("/api/globe/stats", ("GET",), False, False),
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
    ("/post/{post_id}/mark-posted", ("POST",), True, True),
    ("/posts", ("GET",), True, False),
    ("/proof/{article_id}", ("GET",), False, False),
    ("/redoc", ("GET",), False, False),
    ("/stories/{story_id}", ("GET",), False, False),
    ("/story/{story_id}/approve", ("POST",), True, True),
    ("/story/{story_id}/edit", ("GET",), True, False),
    ("/story/{story_id}/reject", ("POST",), True, True),
    ("/story/{story_id}/save", ("POST",), True, True),
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
        assert by_path["/posts"][2] is True
        assert by_path["/api/stories/{story_id}/viewpoints"][2] is True
        assert by_path["/api/stories/{story_id}/sources"][2] is True
        assert by_path["/api/globe/events"][2] is False
        assert by_path["/api/globe/stats"][2] is False
        assert by_path["/api/globe/layers"][2] is False
        assert by_path["/api/map/freshness"][2] is False
        assert by_path["/api/map/stories"][2] is False
        assert by_path["/api/map/replay"][2] is False
        assert by_path["/stories/{story_id}"][2] is False
        assert by_path["/proof/{article_id}"][2] is False
        assert by_path["/map"][2] is False
        assert by_path["/healthz"][2] is False
        assert by_path["/healthz/details"][2] is True

    def test_every_mutating_triage_route_needs_csrf(self):
        csfr = {row[0]: row[3] for row in route_table(app)}
        for path in (
            "/story/{story_id}/approve",
            "/story/{story_id}/reject",
            "/story/{story_id}/save",
            "/post/{post_id}/mark-posted",
        ):
            assert csfr[path] is True, path

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
        assert "/api/globe/stats" in paths
        assert "/api/globe/layers" in paths

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