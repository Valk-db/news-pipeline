"""Two doc guards, because both of the drifts this batch fixed were prose.

The curation redesign (2026-10-02) deleted approve, reject, edit and save, and
with them the `/posts` route. Two documents kept describing the world that
existed before it, and neither was caught by a test:

* `curation_ui/templates/error.html` carried `<a href="/posts">`, a button that
  404'd for every visitor who clicked it.
* `curation_ui/main.py`'s surface map still advertised "queue,
  approve/reject/edit/save, posts".

A docstring cannot fail a test on its own, so each one gets a guard that reads
the doc and compares it with the running app. Both guards are written so that
putting the old text back turns them red (see the mutation notes in the batch
report); a guard that cannot fail is worse than no guard.
"""

import os
import re

import pytest

import curation_ui.main as main_module
from tests.test_route_table import iter_routes, route_table

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(main_module.__file__)), "templates")

# Attributes that put a URL into the page. hx-* are the htmx ones: an hx-get on
# a path that 404s is the same dead link as an href, only louder.
URL_ATTR = re.compile(r'\b(?:href|src|hx-get|hx-post|hx-put|hx-delete)="(/[^"]*)"')

STATIC_PREFIX = "/static/"


def _map_module(line: str) -> str | None:
    """The `module.py` a surface-map line names, or None for any other line."""
    match = re.match(r"\s{2}(\w+\.py)\s{2,}\S", line)
    return match.group(1) if match else None


def template_files() -> list[str]:
    return sorted(
        os.path.join(TEMPLATE_DIR, name)
        for name in os.listdir(TEMPLATE_DIR)
        if name.endswith(".html")
    )


def path_regex(path: str, wildcard: str = "[^/]+") -> re.Pattern:
    """A template-or-route path as a full-match regex.

    Both sides use the same rule: a `{param}` in a route and a `{{ expr }}` in a
    template both match exactly one path segment, so `/story/{{ item.story.id }}`
    matches the route `/story/{story_id}`.
    """
    segments = []
    for segment in path.strip("/").split("/") if path.strip("/") else []:
        if "{{" in segment:
            segments.append(wildcard)
        elif segment.startswith("{") and segment.endswith("}"):
            segments.append(wildcard)
        else:
            segments.append(re.escape(segment))
    return re.compile("/" + "/".join(segments) + "/?" if segments else "/")


def route_paths() -> list[str]:
    return [row[0] for row in route_table(main_module.app)]


def static_mount_paths() -> set[str]:
    """The mount prefixes the app serves itself, e.g. /static.

    Read from app.routes directly: iter_routes() descends through a Mount's
    routes list, so the mount itself never comes out of it.
    """
    from starlette.routing import Mount

    return {route.path for route in main_module.app.routes if isinstance(route, Mount)}


class TestEveryAbsoluteTemplateLinkResolvesToARoute:
    """The error page shipped a /posts button for a route that no longer exists."""

    def test_no_template_links_to_a_path_the_app_does_not_serve(self):
        known = [path_regex(p) for p in route_paths()]
        static = static_mount_paths()
        offenders = []
        for full in template_files():
            with open(full, encoding="utf-8") as handle:
                body = handle.read()
            for match in URL_ATTR.finditer(body):
                target = match.group(1)
                if any(target == p or target.startswith(p + "/") for p in static):
                    continue
                if "{{" in target.strip("/").split("/")[0]:
                    # The whole path is built from a query string; there is
                    # nothing static to check it against.
                    continue
                if not any(rx.fullmatch(target) for rx in known):
                    offenders.append((os.path.basename(full), target))
        assert offenders == []

    def test_the_check_sees_the_links_it_thinks_it_sees(self):
        """Guard the guard: a regex that matched nothing would pass forever.

        The error page is the file this batch's finding came from, and it still
        has two absolute links on it, so this pins that the scan reaches it.
        """
        with open(os.path.join(TEMPLATE_DIR, "error.html"), encoding="utf-8") as handle:
            body = handle.read()
        found = URL_ATTR.findall(body)
        assert "/" in found
        assert "/healthz" in found
        assert not any("/posts" in target for target in found)

    def test_every_absolute_link_in_the_app_is_covered_by_a_route_or_a_mount(self):
        """Documents the assumption the first test rests on."""
        for target in ("/", "/map", "/healthz"):
            assert any(path_regex(p).fullmatch(target) for p in route_paths()), target


class TestReadmeCountsComeFromTheRegistry:
    """A count in a README rots silently; the registry it came from does not.

    The numbers checked here were rewritten in this batch because the ones they
    replaced were stale by several sources. Nothing stopped them rotting the same
    way again except that nobody counted, so the count is now asserted.
    """

    COMPONENT_LINE = re.compile(
        r"tier-1 \((\d+) enabled of (\d+) configured\) \+ tier-2 \((\d+) enabled of (\d+)"
    )

    @staticmethod
    def _readme() -> str:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_component_table_matches_the_source_registry(self):
        from src.ingestion.source_registry import TIER1_SOURCES, TIER2_SOURCES

        claimed = self.COMPONENT_LINE.search(self._readme())
        assert claimed, "the README component row no longer states tier counts"

        def counts(sources):
            # (enabled, configured) -- the order the README states them in.
            return sum(1 for c in sources.values() if c.enabled), len(sources)

        assert tuple(int(g) for g in claimed.groups()) == (
            *counts(TIER1_SOURCES),
            *counts(TIER2_SOURCES),
        )

    def test_the_ownership_group_count_matches_units_py(self):
        """Checked because a previous batch reported this pair as inverted.

        OWNERSHIP_GROUPS is domain -> group, so 123 is the DOMAIN count and 118
        the number of distinct groups. Getting that pair backwards is the exact
        error the earlier docs batch made, so it is pinned here.
        """
        from src.verification.units import OWNERSHIP_GROUPS

        text = self._readme()
        match = re.search(r"\((\d+) domains\s*→\s*(\d+) groups\)", text)
        assert match, "README no longer states the ownership-group counts"
        assert tuple(int(g) for g in match.groups()) == (
            len(OWNERSHIP_GROUPS),
            len(set(OWNERSHIP_GROUPS.values())),
        )

    def test_the_readme_names_no_removed_curation_surface(self):
        text = self._readme()
        for gone in ("/posts", "**A**pprove", "mark-posted"):
            assert gone not in text, gone

    def test_the_readme_does_not_describe_pgvector_as_unavailable(self):
        """The extension shipped in 20261002220200_pgvector_readiness.sql."""
        text = self._readme().lower()
        assert "nothing uses pgvector" not in text
        assert "pgvector available if a column ever needs it" not in text


class TestTheSurfaceMapDescribesTheCodeThatExists:
    """curation_ui/main.py's module map is the map a reader trusts."""

    @staticmethod
    def _mapped_modules() -> set[str]:
        return {
            name
            for name in (_map_module(line) for line in main_module.__doc__.splitlines())
            if name
        }

    def test_every_module_in_the_package_is_on_the_map(self):
        package_dir = os.path.dirname(os.path.abspath(main_module.__file__))
        on_disk = {
            name
            for name in os.listdir(package_dir)
            if name.endswith(".py") and name not in ("__init__.py", "main.py")
        }
        assert on_disk - self._mapped_modules() == set()

    def test_every_module_the_map_names_exists(self):
        package_dir = os.path.dirname(os.path.abspath(main_module.__file__))
        assert self._mapped_modules() - set(os.listdir(package_dir)) == set()

    def test_the_map_names_no_deleted_approve_flow(self):
        """The map lines are the contract; prose around them is not.

        The paragraph below the map that records the removal is allowed to use
        the words it is recording.
        """
        for line in self._map_lines():
            lowered = line.lower()
            for phrase in ("approve", "reject", "/edit", "mark-posted"):
                assert phrase not in lowered, line
            assert "/posts" not in lowered, line

    @staticmethod
    def _map_lines() -> list[str]:
        return [line for line in main_module.__doc__.splitlines() if _map_module(line)]

    @pytest.mark.parametrize(
        "module_name",
        ["curation.py", "story_api.py", "public_pages.py", "globe.py", "map_api.py", "health.py"],
    )
    def test_a_mapped_router_surface_names_a_path_the_router_serves(self, module_name):
        """Each mapped line's paths must exist, so a renamed route shows up here."""
        module = __import__(f"curation_ui.{module_name[:-3]}", fromlist=["router"])
        served = {
            getattr(route, "path", None)
            for route in iter_routes(getattr(module, "router").routes)
        }
        line = next(
            line for line in main_module.__doc__.splitlines() if line.strip().startswith(module_name)
        )
        for mentioned in re.findall(r"/[\w{}/*.-]+", line):
            if "removed" in line:
                # A line that records a removal is not claiming the path is served.
                continue
            if mentioned.endswith("*"):
                prefix = mentioned[:-1]
                assert any(path.startswith(prefix) for path in served), (module_name, mentioned)
                continue
            assert any(
                path_regex(path).fullmatch(mentioned) or path_regex(mentioned).fullmatch(path)
                for path in served
            ), (module_name, mentioned)