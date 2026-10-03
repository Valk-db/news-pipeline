"""A template that links to a route the app does not serve is a 404 with extra steps.

`curation_ui/templates/error.html` shipped a "View Posts" button pointing at
`/posts` long after the curation routes that served it were removed. Nothing
failed: no test broke, no build went red, and the button was only reachable
when something had already gone wrong, which is exactly when a curator is least
likely to notice a second thing is broken. It is the kind of rot that survives a
whole cleanup pass because it is one attribute in one template.

So this is a general guard rather than a pinned fact: every local link in every
template is checked against the routes the app actually registers. Adding a dead
link fails the suite.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/none")

from curation_ui.app_state import BASE_DIR  # noqa: E402
from curation_ui.main import app  # noqa: E402
from tests.test_route_table import iter_routes  # noqa: E402

TEMPLATE_DIR = Path(BASE_DIR) / "templates"
STATIC_DIR = Path(BASE_DIR) / "static"

# href/hx-get/hx-post/hx-delete/action. Deliberately not an HTML parser: the
# templates are hand-written and flat, and every interesting attribute value is
# a quoted string.
LINK_ATTR = re.compile(r'\b(href|hx-get|hx-post|hx-delete|action)\s*=\s*"([^"]*)"')
# The first place a link stops being a fixed path.
JINJA_START = re.compile(r"\{\{|\{%")


def _local_links() -> list[tuple[Path, str, str]]:
    """(template, attribute, value) for every same-origin link."""
    found = []
    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for attr, value in LINK_ATTR.findall(text):
            if value.startswith("/"):
                found.append((path, attr, value))
    return found


def _served() -> list[list[str]]:
    """Every registered route path, split into segments, `/` trimmed to `['']`."""
    return [
        route.path.rstrip("/").split("/")
        for route in iter_routes(app.routes)
        if isinstance(route, APIRoute)
    ]


def _reachable(prefix: str, truncated: bool) -> bool:
    """Is `prefix` a real destination, or the fixed head of one?

    `truncated` says whether a Jinja expression was cut off the end, which is
    the only case where stopping mid-path is legitimate: `/proof/` out of
    `/proof/{{ article.id }}`. A link that is a whole literal path has to match
    a route's segments exactly, so `/posts` matches nothing.
    """
    want = prefix.rstrip("/").split("/")
    for have in _served():
        if not truncated and len(want) != len(have):
            continue
        if len(want) > len(have):
            continue
        if all(w == h or (h.startswith("{") and h.endswith("}")) for w, h in zip(want, have)):
            return True
    return False


def _check(path: Path, attr: str, value: str) -> None:
    if value.startswith("/static/"):
        relative = value[len("/static/") :].split("?")[0]
        assert (STATIC_DIR / relative).is_file(), f"{path.name} {attr}={value} -> missing file"
        return

    cut = JINJA_START.search(value)
    prefix = value[: cut.start()] if cut else value
    truncated = cut is not None
    assert prefix, f"{path.name} {attr}={value} is nothing but a Jinja expression"
    if not _reachable(prefix, truncated):
        raise AssertionError(
            f"{path.name} {attr}={value!r} (fixed prefix {prefix!r}) matches no registered "
            f"route. Served paths: {sorted('/'.join(s) for s in _served())}"
        )


def test_the_templates_actually_contain_links_to_check() -> None:
    # A guard that scans zero links guards nothing. If a refactor moves every
    # link into a macro or a JS file, this fails so the check gets re-pointed
    # rather than quietly passing on an empty set.
    assert len(_local_links()) >= 8, _local_links()


@pytest.mark.parametrize(("template", "attr", "value"), _local_links())
def test_every_local_link_points_at_something_the_app_serves(
    template: Path, attr: str, value: str
) -> None:
    _check(template, attr, value)


def test_the_error_page_offers_only_reachable_escapes() -> None:
    """The specific regression, kept as its own test so the blame is obvious."""
    error_page = TEMPLATE_DIR / "error.html"
    text = error_page.read_text(encoding="utf-8")
    assert "/posts" not in text, (
        "the error page links to /posts, which has not been a route since the "
        "curation flows were removed; the guard above is the general rule"
    )
    # An error page whose only escape is "Try Again" against an app that is
    # already broken is a dead end, so there has to be a second, public one.
    escapes = {
        v for path, _, v in _local_links() if path == error_page and not v.startswith("/static/")
    }
    assert escapes == {"/", "/map"}, f"error.html offers {sorted(escapes)}"
    for value in escapes:
        _check(error_page, "href", value)
