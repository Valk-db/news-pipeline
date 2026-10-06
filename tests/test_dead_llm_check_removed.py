"""The LLM availability check is gone from the app, and this pins that it stays gone.

`curation_ui/app_state.check_llm()` had zero callers, and the one thing keeping
it alive was a single assignment: `app.state.check_llm_available =
check_llm_available` in `curation_ui/main.py`. `app_state.check_llm` was the
only reader of that `app.state` attribute, so the whole chain -- two functions
and an assignment -- was reachable from nothing.

This is a deletion test rather than a behaviour test on purpose. There is no
behaviour left to assert, so the risk is not "the code is broken" but "the code
comes back and nobody notices, because a function that nothing calls is
invisible and harmless-looking." So the assertions are about ABSENCE, and each
one names what would have to be re-added for the deletion to become wrong:

  * the names appear in no module under `curation_ui/`, `api/` or `src/`;
  * `main` no longer puts `check_llm_available` on `app.state`;
  * the sibling database check is untouched, so this cannot pass by deleting
    too much;
  * the app still boots and the database check still answers through
    `app.state`, which is the behaviour the surviving wiring exists for.

The last one is the assertion that makes the other three worth having: a pure
grep proves a string is absent, and nothing about a string's absence says the
module still imports. `curation_ui.main` importing is the thing that would
actually break, because `main` assigns to `app.state` at import time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEAD_NAMES = ("check_llm", "check_llm_available")


def _product_files() -> list[Path]:
    """Every shipped-or-shippable Python file, excluding tests and caches.

    `tests/` is excluded deliberately: a test is allowed to *mention* a removed
    name, and this one does, in this docstring. Only product code is searched.
    """
    out: list[Path] = []
    for top in ("curation_ui", "api", "src"):
        for path in (REPO_ROOT / top).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return sorted(out)


PRODUCT_FILES = _product_files()


def test_the_product_tree_was_actually_searched() -> None:
    """Guards against the absence assertions passing because nothing was read."""
    assert len(PRODUCT_FILES) > 50, "only %d product files found" % len(PRODUCT_FILES)
    assert (REPO_ROOT / "curation_ui" / "main.py") in PRODUCT_FILES
    assert (REPO_ROOT / "curation_ui" / "app_state.py") in PRODUCT_FILES


@pytest.mark.parametrize("name", DEAD_NAMES)
def test_no_product_module_defines_or_calls_the_removed_check(name: str) -> None:
    """A call is the failure that matters; a definition is just as dead."""
    offenders: list[str] = []
    for path in PRODUCT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                offenders.append("%s:%d name %s" % (path.name, node.lineno, name))
            elif isinstance(node, ast.Attribute) and node.attr == name:
                offenders.append("%s:%d attribute %s" % (path.name, node.lineno, name))
            elif isinstance(node, ast.FunctionDef) and node.name == name or isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                offenders.append("%s:%d def %s" % (path.name, node.lineno, name))
    assert offenders == [], (
        "the LLM availability check is back in product code: %s. It had zero callers "
        "-- if something genuinely needs it now, add the caller and a test for the "
        "behaviour, rather than restoring a function nothing reads." % offenders
    )


def test_main_no_longer_puts_the_check_on_app_state() -> None:
    """The assignment is the specific thing that was keeping the chain alive."""
    main_source = (REPO_ROOT / "curation_ui" / "main.py").read_text(encoding="utf-8")
    assert "app.state.check_llm_available" not in main_source, (
        "app.state.check_llm_available is assigned again. Nothing read it: the only "
        "reader was app_state.check_llm, which had no callers of its own."
    )


def test_app_state_still_exposes_the_database_check() -> None:
    """The sibling must survive, or this file could pass by deleting too much."""
    app_state = (REPO_ROOT / "curation_ui" / "app_state.py").read_text(encoding="utf-8")
    for required in ("def check_database(", "def check_database_public(",
                     "def render_error_page("):
        assert required in app_state, "app_state.py lost %s" % required

    main_source = (REPO_ROOT / "curation_ui" / "main.py").read_text(encoding="utf-8")
    assert "app.state.check_database_available = check_database_available" in main_source


def test_the_app_still_imports_and_the_database_check_answers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A grep proves a string is gone; it says nothing about the module importing.

    `main` assigns to `app.state` at import time, so a bad deletion here breaks
    the serverless entry point. Drive the surviving path the way a router does.
    """
    from curation_ui.app_state import check_database, check_database_public
    from curation_ui.main import app

    class _Req:
        def __init__(self, state):
            self.app = type("A", (), {"state": state})()

    # monkeypatch, not a bare assignment, and this is not a style preference.
    # `app` is a module-level singleton, so assigning to its state and restoring
    # nothing leaks into every test that runs later in the session. Measured: with
    # a bare assignment here, the full suite was **79 failed / 2107 passed**,
    # across 5 unrelated files (test_map_public 26, test_proof_permalinks 22,
    # test_discovery 15, test_public_read_hardening 12, test_phase2_isolation 4),
    # every one of them failing because the database check had been left returning
    # False for the rest of the run. The failures are all in files this change does
    # not touch, which is the signature that makes this so easy to misread as a
    # real regression and chase for an hour.
    monkeypatch.setattr(app.state, "check_database_available", lambda: (True, ""))
    assert check_database(_Req(app.state)) == (True, "")
    assert check_database_public(_Req(app.state)) == (True, "")

    monkeypatch.setattr(app.state, "check_database_available",
                        lambda: (False, "Database not configured."))
    ok, message = check_database_public(_Req(app.state))
    assert ok is False
    # The public surface must not publish its own configuration to an anonymous
    # caller. This is the behaviour that justified keeping check_database at all.
    assert "DATABASE_URL" not in message
