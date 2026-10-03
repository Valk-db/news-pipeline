#!/usr/bin/env python3
"""Take a story off the public surfaces, right now, without raw SQL.

    python -m scripts.unpublish_story <story_id> --reason "..." [--actor NAME] [--yes]

Why this exists. Public exposure is human-gated (DECISIONS.md): a story reaches
/map, /stories/{id} and /proof/{id} because a human decided to expose it. The
approve/reject/edit routes that used to be that human were removed in the
2026-10-02 curation redesign, and nothing replaced them. So a story that had
already been exposed could only be withdrawn by hand-editing the database, and
the gap was live: get a story id wrong, get a grouping wrong, or decide after
publishing that it names a person it should not -- and the only available tool
was UPDATE stories SET status = ... typed by hand against production. This
script is that tool, with a confirmation, an audit row, and a refusal to do
anything to a story that was never public.

It sets Story.status to REJECTED. No route changes are needed: every public
filter selects on PUBLIC_STORY_STATUSES (curation_ui/discovery.py), which is
(QUEUED, POSTED), so REJECTED drops out of /api/map/stories, /api/globe/events,
/api/map/replay and /stories/{id} on the next request that reaches the database.

HOW IMMEDIATE IS "ON THE NEXT REQUEST". Not uniformly, and pretending otherwise
would be the most dangerous thing this script could do:

  * /stories/{id}, /proof/{id}, /api/map/stories, /api/globe/events,
    /api/map/replay -- immediate. They read the story's status on every request.
  * the five cached map read APIs (/api/globe/events, /api/globe/layers,
    /api/map/freshness, /api/map/replay, /api/map/stories) carry
    "s-maxage=1800, stale-while-revalidate=3600" (curation_ui/cache.py), so the
    edge can serve a body captured before the withdrawal for up to half an hour,
    and while revalidating for up to an hour past that. The withdrawal is
    committed; the edge just has not noticed yet.

If the withdrawal cannot wait out the cache, the lever is a redeploy, which
drops the data cache. Say so in the output rather than printing "done".

The event endpoints only started honouring story status on 2026-10-03. Until
then /api/globe/events drew a pin for every event whose story was BLOCKED or
PENDING, so setting REJECTED did not take those pins down and this script would
have been a half-measure with a confident README. The filter is in
curation_ui/events.py now and tests/test_public_event_status_filter.py holds it
there.

What it deliberately does NOT do:

  * not delete the story, its units, its links or its media. The evidence stays
    exactly where it is, which is the point of an archive: a withdrawn story is
    still a record of what was reported and then taken back.
  * not touch the Merkle log. That log covers raw_articles, not story status,
    and it is append-only at the database level (trigger plus role grants), so
    there is nothing here to append and no way to remove anything.
  * not unpublish the rest of a viewpoint cluster. It reports the siblings that
    are still public and leaves that call to the operator, because "these three
    were the same allegation" is a judgement, and this script does not make
    judgements about harm.

The audit row goes in status_log (phase "unpublish"), carrying the previous
status, the reason, the actor and whether the story was public before the
change. It does not reuse src.ingestion.run.log_status: that helper lives in the
ingest entry point and reads .git for a commit SHA, and a withdrawal script
should not import the ingest import graph to write one row. commit_sha is
therefore NULL here, which is honest -- this change was not made by a commit.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid
from dataclasses import dataclass, field

# Add project root to path for imports (repo root, not src/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from curation_ui.discovery import PUBLIC_STORY_STATUSES  # noqa: E402
from src.schema.models import StatusLog, Story  # noqa: E402
from src.shared.config import get_settings  # noqa: E402
from src.shared.database import get_session  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1

LOG_PHASE = "unpublish"
WITHDRAWN_STATUS = Story.Status.REJECTED

# The cache window, restated here so the script can print the real number
# instead of a vague "shortly". If MAP_READ_S_MAXAGE changes, this is wrong;
# read it from the module rather than duplicating the value.
try:
    from curation_ui.cache import MAP_READ_S_MAXAGE, MAP_READ_STALE_WHILE_REVALIDATE

    _CACHED_READS = (
        f"up to {MAP_READ_S_MAXAGE // 60} minutes stale, and up to "
        f"{MAP_READ_STALE_WHILE_REVALIDATE // 60} more while revalidating"
    )
except Exception:  # pragma: no cover - cache module always imports in practice
    _CACHED_READS = "cached at the edge for up to half an hour"


@dataclass
class UnpublishResult:
    """What happened, in the words the operator needs to hear.

    `changed` is separate from `was_public` on purpose. A story that is PENDING
    is not public, and marking it REJECTED changes a triage state without
    withdrawing anything from anybody -- reporting those two as the same thing
    would be how an operator comes to believe a withdrawal took effect when it
    withdrew nothing.
    """

    story_id: str
    previous_status: str
    new_status: str
    was_public: bool
    changed: bool
    headline: str | None
    day: str | None
    detail_was_forced: bool = False
    still_public_siblings: list[tuple[str, str]] = field(default_factory=list)
    explanation: str = ""

    def summary_lines(self) -> list[str]:
        lines = [
            f"story        {self.story_id}",
            f"headline     {self.headline or '(no headline)'}",
            f"day          {self.day or '(no day)'}",
            f"was          {self.previous_status} ({'public' if self.was_public else 'not public'})",
        ]
        if not self.changed:
            lines.append(f"unchanged    already {self.previous_status}")
            if self.explanation:
                lines.append(f"reason       {self.explanation}")
            return lines
        lines.append(f"now          {self.new_status} (withdrawn from every public surface)")
        if self.detail_was_forced:
            lines.append(
                "note         this story was not public; --force was passed, so a triage "
                "state was overwritten without withdrawing anything"
            )
        lines.append(f"cached reads {_CACHED_READS} until the edge notices")
        if self.still_public_siblings:
            lines.append(
                f"WARNING      {len(self.still_public_siblings)} other story id(s) in the "
                "same viewpoint cluster are still public:"
            )
            for sid, status in self.still_public_siblings:
                lines.append(f"               {sid}  {status}")
            lines.append("           unpublish them individually if the withdrawal was meant to cover them")
        return lines


def resolve_transition(current_status: str, *, force: bool = False) -> tuple[str, bool, str]:
    """What this script would do to a story in `current_status`.

    Returns (action, requires_force, explanation) where action is one of
    "withdraw", "refuse" or "noop". Pure, so the decision is testable without a
    database -- the whole point of the refusals is that they are not a side
    effect of a query timing out.
    """
    status = Story.Status(current_status)

    if status is WITHDRAWN_STATUS:
        return ("noop", False, "already REJECTED, so no public surface is serving it")

    if status not in PUBLIC_STORY_STATUSES:
        explanation = (
            f"{status.value} is not in PUBLIC_STORY_STATUSES "
            f"({', '.join(s.value for s in PUBLIC_STORY_STATUSES)}), so this story is "
            "already invisible to the public and setting REJECTED would withdraw "
            "nothing -- it would only overwrite a triage decision."
        )
        if not force:
            return ("refuse", True, explanation + " Pass --force if that is what you want.")
        return ("withdraw", False, "forced: " + explanation)

    return ("withdraw", False, "")


def default_actor() -> str:
    """Who is doing this. Recorded either way; a blank actor is not an audit trail."""
    for env_var in ("USER", "USERNAME", "LOGNAME"):
        value = os.environ.get(env_var)
        if value:
            return value
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


async def _still_public_siblings(session, story: Story) -> list[tuple[str, str]]:
    """Other story ids in the same viewpoint cluster that are still public.

    Reported, not acted on. A viewpoint cluster is a set of stories about the
    same event from different angles, so withdrawing one of them and leaving the
    other three up is a very easy way to think the withdrawal was complete when
    it was not.
    """
    if story.viewpoint_cluster_id is None:
        return []
    result = await session.execute(
        select(Story.id, Story.status).where(
            Story.viewpoint_cluster_id == story.viewpoint_cluster_id,
            Story.id != story.id,
            Story.status.in_(PUBLIC_STORY_STATUSES),
        )
    )
    return [(str(row[0]), str(getattr(row[1], "value", row[1]))) for row in result.all()]


async def unpublish_story(
    story_id: str,
    *,
    reason: str,
    actor: str,
    force: bool = False,
    session=None,
) -> UnpublishResult:
    """Set one story to REJECTED and write the audit row.

    `session` exists so the decision and the audit row can be tested without a
    database; the CLI never passes it and always goes through get_session().
    """

    async def run(sess) -> UnpublishResult:
        story = await sess.get(Story, uuid.UUID(story_id))
        if story is None:
            raise LookupError(
                f"no story with id {story_id}. If the id is right, the story may be a "
                "reporting unit or an article id rather than a story id."
            )

        previous = story.status
        action, _, explanation = resolve_transition(previous.value, force=force)
        siblings = await _still_public_siblings(sess, story)
        day = story.day.date().isoformat() if story.day else None

        def result(**overrides) -> UnpublishResult:
            base = {
                "story_id": str(story.id),
                "previous_status": previous.value,
                "new_status": previous.value,
                "was_public": False,
                "changed": False,
                "headline": None,
                "day": day,
                "detail_was_forced": False,
                "still_public_siblings": siblings,
                "explanation": explanation,
            }
            base.update(overrides)
            return UnpublishResult(**base)

        if action == "refuse":
            return result()

        if action == "noop":
            return result(was_public=previous in PUBLIC_STORY_STATUSES)

        story.status = WITHDRAWN_STATUS
        sess.add(
            StatusLog(
                phase=LOG_PHASE,
                status="warn",
                details={
                    "action": "unpublish",
                    "story_id": str(story.id),
                    "previous_status": previous.value,
                    "new_status": WITHDRAWN_STATUS.value,
                    "was_public": previous in PUBLIC_STORY_STATUSES,
                    "forced": bool(force),
                    "reason": reason,
                    "actor": actor,
                    "note": explanation or "withdrawn by hand; no route changes needed",
                },
            )
        )
        await sess.commit()

        return result(
            new_status=WITHDRAWN_STATUS.value,
            was_public=previous in PUBLIC_STORY_STATUSES,
            changed=True,
            detail_was_forced=bool(force),
        )

    if session is not None:
        return await run(session)
    async with get_session() as owned:
        return await run(owned)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.unpublish_story",
        description="Withdraw one story from every public surface by setting it to REJECTED.",
        epilog=(
            "Cached map read APIs (/api/globe/events, /api/map/stories, "
            "/api/map/replay, /api/globe/layers, /api/map/freshness) can serve a "
            "stale body after this. The HTML surfaces reflect the change on the "
            "next request."
        ),
    )
    parser.add_argument("story_id", help="the story UUID to withdraw")
    parser.add_argument(
        "--reason",
        required=True,
        help="why, in one line. Stored in status_log. Not optional: an unlogged "
        "withdrawal cannot be reviewed later.",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="who is doing this. Defaults to $USER, then the OS login name.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (for non-interactive use)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="also overwrite a triage status (PENDING/BLOCKED/EXPIRED) that is "
        "already invisible to the public. Off by default: that withdraws nothing.",
    )
    return parser


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


async def _amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL not configured; nothing to do.", file=sys.stderr)
        return EXIT_ERROR

    actor = args.actor or default_actor()

    if not args.yes:
        print(f"about to withdraw story {args.story_id}")
        print(f"  actor  {actor}")
        print(f"  reason {args.reason}")
        print("  this sets status to REJECTED; the public surfaces drop it on the next request")
        if not _confirm("proceed? [y/N] "):
            print("aborted; nothing was changed")
            return EXIT_OK

    try:
        result = await unpublish_story(
            args.story_id, reason=args.reason, actor=actor, force=args.force
        )
    except LookupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"ERROR: {args.story_id!r} is not a UUID: {exc}", file=sys.stderr)
        return EXIT_ERROR

    for line in result.summary_lines():
        print(line)

    if result.changed:
        print(f"audit row written to status_log (phase={LOG_PHASE}, actor={actor})")
    else:
        print("refused: nothing was changed, and no audit row was written")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.force and not args.yes and not sys.stdin.isatty():
        print(
            "refusing to withdraw without confirmation: stdin is not a terminal. "
            "Pass --yes to make it unconditional.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
