"""The withdrawal tool, tested without a database.

An unpublish script is the thing you reach for when something is already wrong
and there is no time to read the code. That makes its refusals the most important
lines in it: a script that cheerfully sets REJECTED on a story nobody ever
approved, and calls that a successful withdrawal, would look identical in the
output to one that actually took something down.

So the decision table is tested as a table, the audit row is checked field by
field, and the two mistakes that would make the output lie -- reporting a
triage-only change as a withdrawal, and reporting a withdrawal as immediate when
five read APIs are cached at the edge for half an hour -- are pinned.
"""

from __future__ import annotations

import ast
import inspect
import io
import sys
import uuid
from contextlib import redirect_stderr

import pytest

from scripts import unpublish_story as mod
from src.schema.models import StatusLog, Story

STORY_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_ID = uuid.UUID("99999999-8888-7777-6666-555555555555")
CLUSTER_ID = uuid.UUID("cccccccc-0000-0000-0000-000000000000")


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """Just enough AsyncSession for the script: get, execute, add, commit."""

    def __init__(self, story, sibling_rows=()):
        self.story = story
        self.sibling_rows = list(sibling_rows)
        self.added = []
        self.commits = 0
        self.executed = 0

    async def get(self, model, pk):
        assert model is Story
        return self.story if self.story is not None and self.story.id == pk else None

    async def execute(self, stmt):
        self.executed += 1
        return FakeResult(self.sibling_rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def make_story(status=Story.Status.QUEUED, cluster=None):
    import datetime

    return Story(
        id=STORY_ID,
        day=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
        status=status,
        viewpoint_cluster_id=cluster,
        primary_entities=[],
    )


class TestTheDecisionTable:
    """resolve_transition is the whole safety argument, so it is tested as data."""

    @pytest.mark.parametrize("status", [Story.Status.QUEUED, Story.Status.POSTED])
    def test_an_approved_story_is_withdrawn(self, status) -> None:
        action, needs_force, _ = mod.resolve_transition(status.value)
        assert action == "withdraw"
        assert needs_force is False

    @pytest.mark.parametrize(
        "status",
        [Story.Status.PENDING, Story.Status.BLOCKED, Story.Status.EXPIRED],
    )
    def test_a_story_nobody_approved_is_refused(self, status) -> None:
        action, needs_force, explanation = mod.resolve_transition(status.value)
        assert action == "refuse", (
            f"{status.value} is not public; withdrawing it withdraws nothing"
        )
        assert needs_force is True
        assert "--force" in explanation
        assert status.value in explanation

    def test_an_already_rejected_story_is_a_no_op_not_an_error(self) -> None:
        action, needs_force, explanation = mod.resolve_transition(
            Story.Status.REJECTED.value
        )
        assert action == "noop"
        assert needs_force is False
        assert "already REJECTED" in explanation

    @pytest.mark.parametrize(
        "status", [Story.Status.PENDING, Story.Status.BLOCKED, Story.Status.EXPIRED]
    )
    def test_force_overrides_the_refusal(self, status) -> None:
        action, _, explanation = mod.resolve_transition(status.value, force=True)
        assert action == "withdraw"
        assert explanation.startswith("forced:")

    def test_force_does_not_change_the_no_op(self) -> None:
        """Re-running must stay a no-op; --force is not a repair tool."""
        action, _, _ = mod.resolve_transition(Story.Status.REJECTED.value, force=True)
        assert action == "noop"

    def test_an_unknown_status_is_an_error_not_a_silent_withdrawal(self) -> None:
        with pytest.raises(ValueError):
            mod.resolve_transition("archived")

    def test_every_status_the_model_defines_is_decided(self) -> None:
        """No status may fall through the table unhandled."""
        for status in Story.Status:
            action, _, _ = mod.resolve_transition(status.value, force=True)
            assert action in {"withdraw", "noop"}, status

    def test_the_table_tracks_the_public_tuple_rather_than_a_copy(self) -> None:
        source = inspect.getsource(mod.resolve_transition)
        assert "PUBLIC_STORY_STATUSES" in source, (
            "the decision must read the shared tuple, not a list of its own, or "
            "widening the public statuses would not widen what can be withdrawn"
        )
        assert "queued" not in source and "posted" not in source


class TestTheWithdrawal:
    async def test_an_approved_story_becomes_rejected(self) -> None:
        story = make_story(Story.Status.QUEUED)
        session = FakeSession(story)
        result = await mod.unpublish_story(
            str(STORY_ID), reason="wrong grouping", actor="tyler", session=session
        )
        assert result.changed is True
        assert result.was_public is True
        assert result.previous_status == "queued"
        assert result.new_status == "rejected"
        assert story.status is Story.Status.REJECTED
        assert session.commits == 1

    async def test_the_audit_row_records_who_why_and_from_what(self) -> None:
        session = FakeSession(make_story(Story.Status.POSTED))
        await mod.unpublish_story(
            str(STORY_ID), reason="names a person it should not", actor="tyler", session=session
        )
        assert len(session.added) == 1
        row = session.added[0]
        assert isinstance(row, StatusLog)
        assert row.phase == "unpublish"
        details = row.details
        assert details["previous_status"] == "posted"
        assert details["new_status"] == "rejected"
        assert details["was_public"] is True
        assert details["reason"] == "names a person it should not"
        assert details["actor"] == "tyler"
        assert details["story_id"] == str(STORY_ID)
        # A withdrawal with no written reason cannot be reviewed later.
        assert details["reason"].strip()

    async def test_nothing_is_written_when_nothing_changes(self) -> None:
        session = FakeSession(make_story(Story.Status.REJECTED))
        result = await mod.unpublish_story(
            str(STORY_ID), reason="again", actor="tyler", session=session
        )
        assert result.changed is False
        assert session.added == []
        assert session.commits == 0

    async def test_nothing_is_written_for_a_refused_story(self) -> None:
        session = FakeSession(make_story(Story.Status.PENDING))
        result = await mod.unpublish_story(
            str(STORY_ID), reason="early", actor="tyler", session=session
        )
        assert result.changed is False
        assert result.was_public is False
        assert session.added == []
        assert session.commits == 0
        assert "--force" in result.explanation

    async def test_a_refused_story_is_not_reported_as_a_withdrawal(self) -> None:
        """The output must not say 'withdrawn' for something that withdrew nothing."""
        session = FakeSession(make_story(Story.Status.PENDING))
        result = await mod.unpublish_story(
            str(STORY_ID), reason="early", actor="tyler", session=session
        )
        printed = "\n".join(result.summary_lines())
        assert "withdrawn from every public surface" not in printed
        assert "(not public)" in printed
        assert "unchanged" in printed

    async def test_force_does_change_the_row_but_says_so(self) -> None:
        session = FakeSession(make_story(Story.Status.BLOCKED))
        result = await mod.unpublish_story(
            str(STORY_ID), reason="forced", actor="tyler", force=True, session=session
        )
        assert result.changed is True
        assert result.detail_was_forced is True
        assert session.added[0].details["forced"] is True
        assert "without withdrawing anything" in "\n".join(result.summary_lines())

    async def test_an_unknown_id_raises_rather_than_creating_anything(self) -> None:
        session = FakeSession(None)
        with pytest.raises(LookupError):
            await mod.unpublish_story(
                str(OTHER_ID), reason="typo", actor="tyler", session=session
            )
        assert session.added == []

    async def test_a_non_uuid_is_rejected_before_the_query(self) -> None:
        session = FakeSession(make_story())
        with pytest.raises(ValueError):
            await mod.unpublish_story("14c5", reason="x", actor="ty", session=session)
        assert session.executed == 0


class TestSiblingsAreReported:
    async def test_still_public_siblings_are_listed(self) -> None:
        story = make_story(Story.Status.QUEUED, cluster=CLUSTER_ID)
        session = FakeSession(
            story, sibling_rows=[(OTHER_ID, Story.Status.QUEUED)]
        )
        result = await mod.unpublish_story(
            str(STORY_ID), reason="cluster", actor="tyler", session=session
        )
        assert result.still_public_siblings == [(str(OTHER_ID), "queued")]
        printed = "\n".join(result.summary_lines())
        assert "WARNING" in printed
        assert str(OTHER_ID) in printed

    async def test_no_cluster_means_no_query_and_no_warning(self) -> None:
        session = FakeSession(make_story(Story.Status.QUEUED, cluster=None))
        result = await mod.unpublish_story(
            str(STORY_ID), reason="solo", actor="tyler", session=session
        )
        assert result.still_public_siblings == []
        assert session.executed == 0
        assert "WARNING" not in "\n".join(result.summary_lines())

    async def test_the_sibling_query_excludes_the_story_itself(self) -> None:
        """A story is its own cluster member; listing it would be a false alarm."""
        source = inspect.getsource(mod._still_public_siblings)
        assert "Story.id != story.id" in source


class TestTheOutputDoesNotOverclaim:
    def test_the_cache_window_is_stated_with_real_numbers(self) -> None:
        from curation_ui.cache import MAP_READ_S_MAXAGE

        assert str(MAP_READ_S_MAXAGE // 60) in mod._CACHED_READS
        assert "minutes stale" in mod._CACHED_READS

    def test_the_cached_reads_are_named_in_the_help(self) -> None:
        help_text = mod.build_parser().format_help()
        assert "/api/globe/events" in help_text
        assert "stale" in help_text

    def test_the_actor_is_never_blank(self) -> None:
        assert mod.default_actor().strip()

    def test_the_module_docstring_does_not_promise_immediacy(self) -> None:
        doc = mod.__doc__ or ""
        assert "s-maxage=1800" in doc
        assert "redeploy" in doc, "the escape hatch for a stale edge must be documented"


class TestTheCli:
    def test_a_reason_is_required(self) -> None:
        with pytest.raises(SystemExit):
            mod.build_parser().parse_args([str(STORY_ID)])

    def test_the_flags_exist_and_default_off(self) -> None:
        args = mod.build_parser().parse_args([str(STORY_ID), "--reason", "r"])
        assert args.yes is False
        assert args.force is False
        assert args.actor is None

    def test_it_refuses_to_run_unconfirmed_without_a_terminal(self) -> None:
        """A script that prompts and gets EOF must abort, not assume yes."""
        saved = sys.stdin
        sys.stdin = io.StringIO("")  # isatty() False, read() returns ""
        try:
            code = mod.main([str(STORY_ID), "--reason", "r"])
        finally:
            sys.stdin = saved
        assert code == mod.EXIT_ERROR

    def test_it_does_not_need_a_database_to_build_its_parser(self) -> None:
        assert mod.build_parser().prog.endswith("scripts.unpublish_story")

    def test_a_missing_database_is_refused_before_any_query(self, monkeypatch) -> None:
        class NoDb:
            has_database = False

        monkeypatch.setattr(mod, "get_settings", lambda: NoDb())
        err = io.StringIO()
        with redirect_stderr(err):
            code = asyncio_run(mod._amain(_args(story_id=str(STORY_ID))))
        assert code == mod.EXIT_ERROR
        assert "DATABASE_URL" in err.getvalue()


def _args(**kwargs):
    import argparse

    defaults = {
        "story_id": str(STORY_ID),
        "reason": "r",
        "actor": "tyler",
        "yes": True,
        "force": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


class TestNoDeadClaim:
    """The script must not claim a guarantee the code does not provide."""

    def test_it_does_not_say_it_removes_the_story(self) -> None:
        doc = (mod.__doc__ or "").lower()
        assert "not delete the story" in doc

    def test_it_does_not_touch_the_merkle_log(self) -> None:
        source = inspect.getsource(mod)
        assert "merkle" not in source.lower().replace("merkle log. that log", "")

    def test_the_public_status_tuple_is_imported_not_redeclared(self) -> None:
        assert "PUBLIC_STORY_STATUSES = " not in inspect.getsource(mod)

    def test_the_withdrawn_status_is_rejected(self) -> None:
        """Not POSTED, not EXPIRED: EXPIRED is what the cleanup job owns."""
        assert mod.WITHDRAWN_STATUS is Story.Status.REJECTED

    def test_no_status_literal_is_hardcoded_in_the_decision(self) -> None:
        body = ast.parse(inspect.getsource(mod.resolve_transition))
        literals = {
            node.value
            for node in ast.walk(body)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not (literals & set(s.value for s in Story.Status)), (
            f"status names hardcoded in the decision: {literals}"
        )
