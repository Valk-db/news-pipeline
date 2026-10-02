"""Per-feed health across runs, so a feed that yields nothing is reported dead.

Why this file exists: a configured feed that keeps returning nothing used to
be indistinguishable from a feed that quietly works. The GDELT blind-spot audit
counted 16 of 36 configured feed URLs dead (44%) on the day it ran, and nothing
in the pipeline said so at run time -- no error, no warning, just a smaller
article count that reads like a quiet news day. A feed can get there three ways:
its URL rots and the fetch 404s, it starts returning an empty feed, or it
returns entries whose article pages all 403 (which is how scmp.com behaved on
2026-10-02: 50 entries, 0 articles, no error anywhere). All three look the same
from outside the process, and all three are invisible for ever if you only look
at whether the fetch raised.

So the counter is per feed URL, persisted between runs, and the verdict is
stated rather than inferred: after DEAD_AFTER_EMPTY_POLLS consecutive polls
that produced no entries at all, the feed is DEAD and is reported as such --
printed by the ingestion run, recorded in the run results, and readable on its
own through report_dead_feeds().

What this deliberately does not do: disable anything. A dead verdict is a
report, not an edit of the registry; a feed that comes back to life goes back to
"ok" on the next poll that yields entries, and that transition is recorded too
(``recovered``), because a monitor that never clears its own alarm is one you
stop reading.

State lives in a JSON file (default var/feed_health.json, overridable with
FEED_HEALTH_STATE_PATH) rather than in the database, because it describes the
fetcher's view of the world, is cheap to lose, and must not be a reason for a
run to fail: every read and write here degrades to in-memory on IO error.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Three consecutive empty polls is the point where "this feed is quiet today"
# stops being a reasonable reading. A weekly-cadence feed can legitimately yield
# nothing on a poll or two; three in a row means the pipeline has nothing from
# this URL and has had nothing for three runs.
DEAD_AFTER_EMPTY_POLLS = 3

DEFAULT_STATE_PATH = "var/feed_health.json"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_DEAD = "dead"


def state_path(path: str | None = None) -> str:
    """Where feed health is persisted. Env wins over the argument over the default."""
    if path:
        return path
    return os.environ.get("FEED_HEALTH_STATE_PATH") or DEFAULT_STATE_PATH


@dataclass
class FeedHealth:
    """What the fetcher has seen from one feed URL, across runs."""

    url: str
    source_key: str = ""
    polls: int = 0
    empty_polls: int = 0
    consecutive_empty: int = 0
    entries_total: int = 0
    last_entries: int = 0
    last_poll_at: str | None = None
    last_yield_at: str | None = None
    last_status: str | None = None
    last_detail: str | None = None
    dead_since: str | None = None
    recovered_count: int = 0

    @property
    def status(self) -> str:
        """The verdict, derived rather than stored so it cannot drift from the counts."""
        if self.consecutive_empty >= DEAD_AFTER_EMPTY_POLLS:
            return STATUS_DEAD
        if self.consecutive_empty:
            return STATUS_EMPTY
        return STATUS_OK

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FeedHealth":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclass
class HealthRegistry:
    """All feed health, plus the bookkeeping the load/save pair needs."""

    feeds: dict[str, FeedHealth] = field(default_factory=dict)

    def get(self, url: str) -> FeedHealth:
        feed = self.feeds.get(url)
        if feed is None:
            feed = FeedHealth(url=url)
            self.feeds[url] = feed
        return feed

    def dead(self) -> list[FeedHealth]:
        return [f for f in self.feeds.values() if f.status == STATUS_DEAD]

    def empty(self) -> list[FeedHealth]:
        return [f for f in self.feeds.values() if f.status == STATUS_EMPTY]

    def to_dict(self) -> dict:
        return {url: f.to_dict() for url, f in self.feeds.items()}


def load_registry(path: str | None = None) -> HealthRegistry:
    """Read persisted health. A missing or unreadable file is an empty registry,
    never an exception: a corrupt monitor file must not stop ingestion."""
    target = state_path(path)
    try:
        with open(target, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return HealthRegistry()
    except (OSError, ValueError) as exc:
        logger.warning("feed health state unreadable at %s (%s); starting empty", target, exc)
        return HealthRegistry()
    if not isinstance(raw, dict):
        return HealthRegistry()
    feeds = {}
    for url, data in raw.items():
        if isinstance(data, dict):
            try:
                feeds[url] = FeedHealth.from_dict(data)
            except TypeError as exc:
                logger.warning("skipping malformed feed health entry for %s: %s", url, exc)
    return HealthRegistry(feeds=feeds)


def save_registry(registry: HealthRegistry, path: str | None = None) -> bool:
    """Persist health atomically. Returns False (and says so) rather than raising."""
    target = state_path(path)
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        # Same temp-then-rename dance as the rest of the repo's JSON state: a
        # run killed mid-write must not leave a truncated file that reads as
        # "no feeds have ever been polled".
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(target) or ".", delete=False
        ) as tmp:
            json.dump(registry.to_dict(), tmp, indent=2, sort_keys=True)
            tmp_path = tmp.name
        os.replace(tmp_path, target)
        return True
    except OSError as exc:
        logger.warning("could not write feed health state to %s: %s", target, exc)
        return False


# The ingestion path records into one process-wide registry, the same way
# IngestStats keeps one process-wide STATS. Threading a registry through
# fetch_feed -> ingest_rss_feeds -> RssAdapter -> run_ingestion to reach it would
# add a parameter to every layer for no gain; the single writer is the fetcher
# and the readers are the run summary and the reporting script.
_ACTIVE = HealthRegistry()


def active() -> HealthRegistry:
    """The registry the ingestion path records into."""
    return _ACTIVE


def load_active(path: str | None = None) -> HealthRegistry:
    """Load persisted health into the process-wide registry and return it."""
    global _ACTIVE
    _ACTIVE = load_registry(path)
    return _ACTIVE


def save_active(path: str | None = None) -> bool:
    """Persist the process-wide registry. False means it could not be written."""
    return save_registry(_ACTIVE, path)


def record_poll(
    registry: HealthRegistry,
    url: str,
    entries: int,
    source_key: str = "",
    detail: str | None = None,
    now: datetime | None = None,
) -> FeedHealth:
    """Record one poll of one feed URL and return its updated health.

    ``entries`` is what the feed yielded, not what was ingested: a feed whose
    article pages all 403 is the case this module exists for, and counting
    ingested articles instead would report that feed as perfectly healthy.
    ``detail`` carries why a poll yielded nothing (a fetch error's stat key, or
    "0 entries"), so the dead report says why rather than just that.
    """
    now = now or datetime.now(timezone.utc)
    feed = registry.get(url)
    if source_key:
        feed.source_key = source_key
    was_dead = feed.status == STATUS_DEAD

    feed.polls += 1
    feed.last_poll_at = now.isoformat()
    feed.last_entries = entries
    feed.last_detail = detail
    if entries > 0:
        feed.entries_total += entries
        feed.consecutive_empty = 0
        feed.last_yield_at = now.isoformat()
        if was_dead:
            feed.recovered_count += 1
        feed.dead_since = None
    else:
        feed.empty_polls += 1
        feed.consecutive_empty += 1
        if feed.status == STATUS_DEAD and feed.dead_since is None:
            feed.dead_since = now.isoformat()

    feed.last_status = feed.status
    return feed


def report_dead_feeds(registry: HealthRegistry) -> list[dict]:
    """The dead feeds, as plain dicts for a run result or a status endpoint."""
    return [
        {
            "url": f.url,
            "source_key": f.source_key,
            "consecutive_empty": f.consecutive_empty,
            "empty_polls": f.empty_polls,
            "polls": f.polls,
            "entries_total": f.entries_total,
            "dead_since": f.dead_since,
            "last_detail": f.last_detail,
        }
        for f in sorted(registry.dead(), key=lambda x: x.url)
    ]


def render_health_report(registry: HealthRegistry) -> str:
    """Markdown table of every feed's verdict. Empty registry says so plainly."""
    if not registry.feeds:
        return "_No feed health recorded yet._"

    rows = sorted(registry.feeds.values(), key=lambda f: (f.status != STATUS_DEAD, f.url))
    lines = [
        "| feed | source | status | polls | consecutive empty | entries seen | last detail |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for f in rows:
        lines.append(
            f"| {f.url} | {f.source_key} | {f.status} | {f.polls} | "
            f"{f.consecutive_empty} | {f.entries_total} | {f.last_detail or ''} |"
        )
    return "\n".join(lines)