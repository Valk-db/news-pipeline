"""The `__new__`-built LLMClient fixtures must know every field `__init__` sets.

This is not a style test. `tests/test_snippet_extractor_live_body.py` builds its
client with `LLMClient.__new__(LLMClient)` and hand-writes the instance fields, so
a field added to `__init__` and not to the fixture does not fail AT THE FIXTURE.
It fails *inside the code under test* with an AttributeError, which reads as a
product bug: when the per-rung budget dict and the sticky demotion set arrived,
13 tests in that file went red reporting "extraction returned nothing" for a reason
that had nothing to do with extraction.

The same failure has a second form, and it is the one this file's token caps hit
first. A hand-written `settings` namespace that is missing a rung's cap attribute
does not raise -- `_cap()` falls back to its default, and the default for a cap is
0, and a cap of 0 refuses everything. So thirteen tests reported "the provider
failed" when the truth was "this fixture has no token cap configured". Both forms
are checked here, and both are derived from `__init__` and ROSTER rather than from
a list somebody has to remember to update.
"""

from __future__ import annotations

import ast
import dis
import inspect
from pathlib import Path

import pytest

from src.shared.llm import LLMClient
from src.shared.llm_roster import ROSTER

_REPO = Path(__file__).resolve().parents[1]


def _fields_set_by_init() -> set[str]:
    """Every `self.<name> = ...` that LLMClient.__init__ performs, directly."""
    found: set[str] = set()
    for instruction in dis.get_instructions(LLMClient.__init__.__code__):
        if instruction.opname == "STORE_ATTR" and isinstance(instruction.argval, str):
            found.add(instruction.argval)
    return found


def _client_assignments(path: Path) -> set[str]:
    """`client.<name> = ...` in a fixture, including annotated assignments.

    `client._budgets: dict = {}` is an ast.AnnAssign, not an ast.Assign. Walking
    only Assign silently skipped every annotated field in the fixture -- which is
    every field in it, so the check passed while checking nothing.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        target: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "client"
        ):
            found.add(target.attr)
    return found


def _settings_namespace_keys(path: Path) -> set[str]:
    """Keys named in the fixture's `client.settings = SimpleNamespace(...)` call.

    Bound to the ASSIGNMENT, not to the first SimpleNamespace in the file. The
    extractor fixture builds several (the fake transport's message, choices and
    usage objects are all SimpleNamespaces), and taking the first one finds the
    transport's, reports none of the settings keys, and fails for the wrong
    reason -- which is worse than not checking, because it looks like the guard
    working.
    """
    tree = ast.parse(path.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if not (
                isinstance(target, ast.Attribute)
                and target.attr == "settings"
                and isinstance(target.value, ast.Name)
                and target.value.id == "client"
            ):
                continue
            call = node.value
            if isinstance(call, ast.Call):
                keys.update(kw.arg for kw in call.keywords if kw.arg)
    return keys


def _hand_built_fixtures() -> list[Path]:
    """Every test file that builds the client with __new__ AND hand-writes settings."""
    out = []
    this_file = Path(__file__).resolve()
    for path in sorted((_REPO / "tests").glob("test_*.py")):
        if path.resolve() == this_file:
            # This file names "LLMClient.__new__" and "client.settings =" in its own
            # prose and would otherwise be scanned as a fixture of itself.
            continue
        source = path.read_text()
        if "LLMClient.__new__" in source and "client.settings =" in source:
            out.append(path)
    return out


def _roster_cap_attrs() -> set[str]:
    """Every DAILY cap attribute on every rung. Minute caps deliberately excluded.

    The distinction is the whole point: `_cap(rung, attr, default=0)` uses 0 as the
    default for both, but 0 means opposite things. A missing DAILY cap is a cap of
    zero, which refuses every call. A missing minute cap is a MinuteLimiter of 0,
    which disables limiting -- permissive, and invisible. Requiring the minute ones
    here would be noise that trains the next reader to ignore this test.
    """
    attrs: set[str] = set()
    for rung in ROSTER:
        for attr in (rung.daily_cap_attr, rung.daily_token_cap_attr):
            if attr:
                attrs.add(attr)
    return attrs


HAND_BUILT = _hand_built_fixtures()


def test_the_scan_actually_finds_fixtures() -> None:
    """Control. A guard that silently finds nothing passes forever.

    If the fixture is ever refactored to use the real constructor this test says
    so loudly, instead of every other test here quietly skipping.
    """
    assert HAND_BUILT, "no __new__-built LLMClient fixture found; the checks below would pass vacuously"


@pytest.mark.parametrize("path", HAND_BUILT, ids=lambda p: p.name)
def test_fixture_knows_every_init_field(path: Path) -> None:
    written = _client_assignments(path)
    missing = sorted(_fields_set_by_init() - written)
    assert not missing, (
        f"{path.name} builds LLMClient with __new__ and is missing {missing}. A "
        f"field missing here fails inside the code under test, not at the fixture."
    )


@pytest.mark.parametrize("path", HAND_BUILT, ids=lambda p: p.name)
def test_fixture_configures_every_roster_cap(path: Path) -> None:
    """A missing cap attribute is a cap of 0, and a cap of 0 refuses everything.

    `_cap(rung, attr, default=0)` returns its default when the attribute is
    absent, so a hand-written settings namespace that forgets `groq_daily_token_cap`
    does not raise -- it configures a zero cap, and every call is then refused as
    "budget exhausted". That is the failure this batch actually hit: 13 tests
    reporting an extraction failure while the provider was never asked.
    """
    configured = _settings_namespace_keys(path)
    missing = sorted(_roster_cap_attrs() - configured)
    assert not missing, (
        f"{path.name} hand-builds client.settings and is missing {missing}. _cap() "
        f"defaults a missing cap to 0, so these silently refuse every call."
    )


def test_init_sets_the_token_budgets_field() -> None:
    """The field the token caps need, named explicitly so its removal is visible."""
    assert "_token_budgets" in _fields_set_by_init()


def test_every_rung_has_a_token_cap_attribute() -> None:
    """A rung with no token cap is a rung whose token spend is uncapped and unreadable.

    `daily_token_cap_attr` is empty on the dataclass so an old pickled rung or a
    hand-built one still constructs, and `_check_token_headroom` skips on empty --
    which means forgetting to set it is silent. Pin it against ROSTER itself.
    """
    assert [r.name for r in ROSTER if not r.daily_token_cap_attr] == []


def test_the_guards_are_not_vacuous() -> None:
    """If either bytecode walk stops finding things, every other test passes."""
    assert len(_fields_set_by_init()) >= 9
    assert len(_roster_cap_attrs()) == 8  # 4 rungs x request cap + token cap
    assert inspect.isfunction(LLMClient.__init__)