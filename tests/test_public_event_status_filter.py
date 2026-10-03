"""The public event endpoints must only draw pins for stories a human approved.

This is a guard against a bug that was live on main and measured, not
theoretical. On dev, 2026-10-03, with the fix absent:

    GET /api/globe/events?hours=0&min_tier1_sources=0
      -> 200, 26 features, 26 distinct story ids
         24 of those stories were BLOCKED  (gate_reason: "Only 0 tier-1 units
                                              (need >=2)")
          2 were PENDING   (in the triage queue, nobody had looked at them)
          0 were QUEUED or POSTED
    GET /stories/{id} for those same ids -> 404

So the public flat map was drawing a pin for every event whose story had failed
the corroboration gate, while the story page for the same story 404'd. That
contradicts the human-gated exposure decision in DECISIONS.md outright: nothing
auto-publishes, except it did, on the map.

The filter therefore lives in curation_ui.events._event_conditions, which both
/api/globe/events and /api/map/replay build their WHERE clause from, so the two
cannot drift apart on it. These tests pin that placement rather than the
symptom, because the symptom needs a populated database and the placement is
what actually guarantees it.
"""

from __future__ import annotations

import inspect
import re

import pytest
from sqlalchemy import and_

from curation_ui import events as events_module
from curation_ui.discovery import PUBLIC_STORY_STATUSES
from src.schema.models import Event, Story

REPO_ROOT = inspect.getfile(events_module)
EVENTS_SOURCE = inspect.getsource(events_module)


def _sql(*args, **kwargs) -> str:
    """The rendered WHERE clause, with values inlined so it can be read."""
    conditions = events_module._event_conditions(*args, **kwargs)
    assert conditions, "_event_conditions returned no conditions at all"
    return str(and_(*conditions).compile(compile_kwargs={"literal_binds": True}))


class TestTheFilterIsThere:
    def test_the_clause_mentions_story_status(self) -> None:
        sql = _sql()
        assert "stories.status" in sql, f"no story-status filter in: {sql}"

    @pytest.mark.parametrize("status", [s.name for s in PUBLIC_STORY_STATUSES])
    def test_each_public_status_is_allowed_through(self, status: str) -> None:
        # The rendered labels are the enum NAMES, not the values: the column is a
        # Postgres enum whose labels are upper case, and SQLAlchemy's default
        # Enum binds names. Checked against the model's own column, not a
        # hardcoded string, so a rename shows up here instead of silently making
        # the filter match nothing.
        assert f"'{status}'" in _sql()

    @pytest.mark.parametrize("status", ["PENDING", "BLOCKED", "REJECTED", "EXPIRED"])
    def test_no_non_public_status_is_allowed_through(self, status: str) -> None:
        assert f"'{status}'" not in _sql(), (
            f"{status} must not reach the public map; PUBLIC_STORY_STATUSES is "
            f"{[s.name for s in PUBLIC_STORY_STATUSES]}"
        )

    def test_it_filters_on_the_story_not_on_the_event(self) -> None:
        """The event row is not what carries the approval; the story row is."""
        sql = _sql()
        assert "events.story_id IN" in sql
        assert "events.status" not in sql, "events has no status column; do not invent one"


class TestTheFilterIsInTheRightPlace:
    """Both public event reads build their clause here, so both are covered."""

    @pytest.mark.parametrize(
        "module_name",
        ["curation_ui.globe", "curation_ui.map_api"],
    )
    def test_the_public_event_endpoints_call_the_shared_builder(self, module_name: str) -> None:
        source = inspect.getsource(__import__(module_name, fromlist=["_"]))
        assert "_event_conditions(" in source, (
            f"{module_name} must build its event WHERE clause with "
            "_event_conditions, or the status filter above stops applying to it"
        )

    def test_nobody_reimplements_the_clause_locally(self) -> None:
        """A second, hand-rolled event query is how the filter gets bypassed."""
        for module_name in ("curation_ui.globe", "curation_ui.map_api"):
            source = inspect.getsource(__import__(module_name, fromlist=["_"]))
            # IS_CANONICAL_EVENT is the other thing the shared builder adds, so a
            # module that references it directly has copied the clause out.
            assert "IS_CANONICAL_EVENT" not in source, (
                f"{module_name} references IS_CANONICAL_EVENT directly; the canonical "
                "filter belongs in _event_conditions with the status filter"
            )

    def test_the_builder_docstring_names_the_measurement(self) -> None:
        doc = events_module._event_conditions.__doc__ or ""
        assert "dev 2026-10-03" in doc, (
            "the docstring should keep the date and the measured counts, so a "
            "future reader knows the filter was added in response to something real"
        )
        assert "26" in doc and "BLOCKED" in doc and "PENDING" in doc


class TestTheAssumptionTheFilterRests:
    def test_every_event_has_a_story_to_be_filtered_on(self) -> None:
        """events.story_id is NOT NULL with a FK, so the subquery drops nothing.

        If this ever becomes nullable, a NULL story_id would be excluded by the
        `IN` and an event would silently vanish from the map -- which is a
        different bug, but one that would be blamed on this filter.
        """
        column = Event.__table__.c.story_id
        assert column.nullable is False
        assert len(column.foreign_keys) == 1
        assert list(column.foreign_keys)[0].column is Story.__table__.c.id

    def test_the_public_status_tuple_is_not_widened_silently(self) -> None:
        """A one-line change here changes what the public map shows.

        It is allowed -- but it has to change the number of approved statuses,
        not reorder them or drop one quietly. This is a canary, not a lock: the
        decision that these two are the public statuses is in DECISIONS.md and is
        Tyler's to make, not this test's.
        """
        assert [s.value for s in PUBLIC_STORY_STATUSES] == ["queued", "posted"]


class TestTheOtherPublicReadsStillFilter:
    """Canary: the other two public story reads were never the problem.

    They filter on PUBLIC_STORY_STATUSES directly. If a future change replaces
    that with a literal tuple or drops it, this fails and someone has to look.
    """

    @pytest.mark.parametrize(
        "module_name", ["curation_ui.public_pages", "curation_ui.map_api"]
    )
    def test_the_module_still_names_the_shared_tuple(self, module_name: str) -> None:
        source = inspect.getsource(__import__(module_name, fromlist=["_"]))
        assert "PUBLIC_STORY_STATUSES" in source, (
            f"{module_name} serves stories to anonymous callers; it must keep "
            "filtering on the shared PUBLIC_STORY_STATUSES tuple"
        )

    # Modules with /api routes that serve story data to a caller who is not
    # authenticated. Each exclusion carries the reason it is safe, so adding a
    # new public route fails this test instead of quietly shipping unapproved
    # stories. Re-check the reason when the module's routes change.
    NOT_PUBLIC_BUT_SERVES_API = {
        "cache.py": "no routes; it is the edge-cache middleware and its allowlist",
        "cron.py": "the two cron routes check a bearer token in the handler",
        "story_api.py": (
            "both routes are Depends(require_auth); the module docstring says so"
        ),
        "events.py": "no routes; it is the shared clause builder these tests cover",
    }

    def test_no_module_serves_stories_publicly_without_the_tuple(self) -> None:
        import pathlib

        package = pathlib.Path(inspect.getfile(events_module)).parent
        seen = set()
        suspicious = []
        for path in sorted(package.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if '"/api/' not in text and "'/api/" not in text:
                continue
            seen.add(path.name)
            if "PUBLIC_STORY_STATUSES" in text or "globe" in path.name:
                continue
            if path.name in self.NOT_PUBLIC_BUT_SERVES_API:
                continue
            suspicious.append(path.name)

        assert not suspicious, (
            f"these modules serve /api routes and never mention "
            f"PUBLIC_STORY_STATUSES: {suspicious}. Check whether they expose "
            "story data that no human approved. If they are authenticated, add "
            "them to NOT_PUBLIC_BUT_SERVES_API with the reason."
        )
        # The list must stay current, or the exemption becomes a place to hide.
        for name in self.NOT_PUBLIC_BUT_SERVES_API:
            if name == "events.py":
                continue  # it has no route string of its own
            assert name in seen, (
                f"{name} no longer serves any /api route; drop it from the "
                "exemption list so the list keeps meaning something"
            )


def test_the_fix_was_not_a_no_op() -> None:
    """Sanity: the filter really is a condition and not a comment."""
    sql = _sql()
    assert re.search(r"stories\.status IN", sql), sql
