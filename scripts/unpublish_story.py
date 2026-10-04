#!/usr/bin/env python
"""Take one story off the public surfaces immediately.

The curation redesign removed approve/reject, so there is no longer a button that
takes a published story down. A story approved before that deletion can still be
served by `/stories/{id}` and `/api/map/stories`, and the only way to retract it
was raw SQL against the production database. This script is that capability, as a
checked command instead of a hand-typed UPDATE.

It sets `stories.status = REJECTED`. That is enough because the public surfaces
read only `PUBLIC_STORY_STATUSES` (QUEUED, POSTED) -- `curation_ui/discovery.py:54`,
honoured by `curation_ui/public_pages.py:61` and `curation_ui/map_api.py:316`. No
route change is needed, and none is made here: unpublishing works *because* the
existing public-status filter already excludes REJECTED.

Every action appends one `status_log` row through the same `log_status()` helper the
ingestion pipeline uses (`src/ingestion/run.py:146`), so the retraction is
attributable: who, when, and why. `--reason` is required for a real write for exactly
that reason -- a log entry that cannot answer "why" is the gap this closes, not a
solution to it.

Usage:
    uv run python scripts/unpublish_story.py <story-id> --dry-run
    uv run python scripts/unpublish_story.py <story-id> --reason "named party, unverified"
    uv run python scripts/unpublish_story.py <story-id> --reason "duplicate of <id>" --actor procmon

Exit codes (so a wrapper script can tell a retraction from a no-op):
    0  unpublished, or already REJECTED (an explicit no-op -- nothing was written)
    1  no such story
    2  usage error (missing id, id is not a UUID, missing --reason)
    3  no database configured, or the engine could not be built

This does NOT satisfy Phase 2 exit criterion #4, which asks for an automatic
per-request flag. This is the manual version; that one is a route-level switch.
"""

import argparse
import asyncio
import getpass
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from src.ingestion.run import log_status
from src.schema.models import Story
from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker

# StatusLog.status is String(20) and the pipeline writes "ok" for a run that did
# what it said (src/ingestion/run.py:615). An unpublish that reported anything else
# would read as a failed retraction.
LOG_STATUS_OK = "ok"
LOG_PHASE = "unpublish"

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_NO_DATABASE = 3


class StoryNotFound(Exception):
    """No story has this id. Distinct from every other refusal, on purpose."""

    def __init__(self, story_id: uuid.UUID):
        self.story_id = story_id
        super().__init__(f"no story with id {story_id}")


class UnpublishResult:
    """What the tool did (or would have done) to one story.

    `changed` is the decision; `wrote` is whether anything was persisted. They
    differ only under --dry-run, and a caller that ignores `wrote` will believe a
    preview unpublished something.
    """

    def __init__(self, story_id, previous_status, changed, wrote, reason, actor):
        self.story_id = story_id
        self.previous_status = previous_status
        self.changed = changed
        self.wrote = wrote
        self.reason = reason
        self.actor = actor

    @property
    def already_rejected(self) -> bool:
        return not self.changed


def _default_actor() -> str:
    """The OS user, or an honest placeholder.

    This tool runs under pressure, so it must not crash before writing the log row
    just because the container has no passwd entry. "unknown" is a worse audit
    answer than a username but a better one than no retraction at all.
    """
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _story_id_arg(raw: str) -> uuid.UUID:
    """argparse type: accept only a real story id, and say so when it is not one.

    `stories.id` is a UUID column (src/schema/models.py:156), so an integer is not
    a malformed-but-fixable id, it is the wrong kind of value -- an operator who
    pastes a row number must be told that rather than have it looked up and found
    missing.
    """
    try:
        return uuid.UUID(raw.strip())
    except (ValueError, AttributeError, TypeError):
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a valid story id: stories.id is a UUID "
            f"(e.g. 3f2504e0-4f89-11d3-9a0c-0305e82c3301), not a row number"
        )


async def unpublish_story(session, story_id: uuid.UUID, *, reason: str, actor: str,
                         dry_run: bool = False) -> UnpublishResult:
    """Set one story to REJECTED and append the status_log row that explains why.

    Idempotent by status, not by argument: a story that is already REJECTED returns
    without touching the row and without a second log entry, so re-running this
    after a partial failure is safe and leaves the ledger with one action per
    retraction rather than two.

    The status write and the log row share one transaction. `log_status()` commits,
    so the story's UPDATE and the StatusLog INSERT flush together: a retraction
    without its audit row, or an audit row for a retraction that did not happen, are
    both unrepresentable.
    """
    story = (
        await session.execute(select(Story).where(Story.id == story_id))
    ).scalar_one_or_none()
    if story is None:
        raise StoryNotFound(story_id)

    previous_status = story.status

    if previous_status == Story.Status.REJECTED:
        return UnpublishResult(story_id, previous_status, changed=False, wrote=False,
                               reason=reason, actor=actor)

    if dry_run:
        return UnpublishResult(story_id, previous_status, changed=True, wrote=False,
                               reason=reason, actor=actor)

    story.status = Story.Status.REJECTED
    await log_status(
        session,
        LOG_PHASE,
        LOG_STATUS_OK,
        {
            "tool": "scripts/unpublish_story.py",
            "story_id": str(story.id),
            "actor": actor,
            "reason": reason,
            "previous_status": previous_status.value,
            "new_status": Story.Status.REJECTED.value,
            "dry_run": False,
        },
    )
    return UnpublishResult(story_id, previous_status, changed=True, wrote=True,
                           reason=reason, actor=actor)


def _describe(result: UnpublishResult, dry_run: bool) -> str:
    """The operator-facing lines. Silence here is how a retraction goes unnoticed."""
    lines = [f"Story {result.story_id}"]
    lines.append(f"  status: {result.previous_status.value} -> {Story.Status.REJECTED.value}")
    if dry_run:
        lines.append("  DRY RUN: nothing was written. Re-run without --dry-run to apply.")
        return "\n".join(lines)
    if not result.changed:
        lines.append("  Already REJECTED: no-op. Nothing was written.")
        return "\n".join(lines)
    lines.append(f"  logged: phase={LOG_PHASE} actor={result.actor!r} reason={result.reason!r}")
    lines.append("  Public surfaces now exclude it (PUBLIC_STORY_STATUSES omits REJECTED).")
    return "\n".join(lines)


async def run(story_id: uuid.UUID, *, reason: str, actor: str, dry_run: bool = False) -> int:
    """Open a session, unpublish, print what happened. Returns the exit code."""
    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL not configured; cannot unpublish anything.")
        print("       Exit 3 without a database -- this tool never pretends to have run.")
        return EXIT_NO_DATABASE

    engine = _get_engine()
    if engine is None:
        print("ERROR: could not create a database engine. Nothing was written.")
        return EXIT_NO_DATABASE

    session_maker = _get_session_maker()
    if session_maker is None:
        print("ERROR: could not create a session maker. Nothing was written.")
        return EXIT_NO_DATABASE

    async with session_maker() as session:
        try:
            result = await unpublish_story(
                session, story_id, reason=reason, actor=actor, dry_run=dry_run
            )
        except StoryNotFound as exc:
            print(f"ERROR: {exc}. Nothing was written.")
            return EXIT_NOT_FOUND

    print(_describe(result, dry_run))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unpublish_story.py",
        description=(
            "Set one story to REJECTED so the public surfaces stop serving it, and "
            "append the status_log row that records who did it and why."
        ),
        epilog=(
            "Exit codes: 0 unpublished or already-REJECTED no-op, 1 no such story, "
            "2 usage error, 3 no database configured."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "story_id",
        type=_story_id_arg,
        help="UUID of the story to unpublish (stories.id is a UUID column).",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help=(
            "Why this story is being taken down. Required for a real write: it is the "
            "only thing that makes the status_log row answer 'why'. Optional with "
            "--dry-run, where nothing is logged."
        ),
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="Who is taking it down; recorded in the log. Defaults to the OS user.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.reason is None or not args.reason.strip():
        if not args.dry_run:
            parser.error(
                "--reason is required to unpublish: the status_log row is the audit "
                "trail for a retraction, and an empty reason makes it unanswerable. "
                "Pass --reason \"...\" (or --dry-run to preview without one)."
            )
    reason = (args.reason or "").strip() or "(none given -- dry run)"

    actor = args.actor or _default_actor()
    return asyncio.run(
        run(args.story_id, reason=reason, actor=actor, dry_run=args.dry_run)
    )


if __name__ == "__main__":
    raise SystemExit(main())