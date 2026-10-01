"""RSS evidence locker: polite feed polling, full article archive, Merkle stamping.

GDELT is the radar -- breadth, machine events, no article text. RSS is the
evidence -- depth, the publisher's own bytes. This module is the evidence half:
it polls a small set of hand-verified tier-1 feeds, fetches the full article
text for each new item, stores the body with a content hash, and appends one
observation per article to the hash-chained transparency log. A GDELT radar
article and an RSS evidence article about the same event are then joined by
canonical URL: url_hash is the SHA256 of the u1 canonical form, so matching
url_hashes IS canonical URL matching, and the join is an exact identity rather
than a similarity guess.

Three properties are deliberate and worth stating up front.

Politeness is the point, not an optimization. Transport is urllib only. httpx
and raw http.client both bypass or break against the egress proxy this code runs
behind, and urllib honors it. Every request carries an honest User-Agent, a
20 second timeout, and the validators the server gave us last time, so an
unchanged feed costs one 304 instead of a full download. Cadence is enforced in
code by should_poll(), not by the scheduler: 900s normally, 600s after a poll
that actually yielded items, 3600s once three consecutive polls came back 304.
A 429 or 503 is honored with Retry-After, and everything that fails backs off
exponentially from 60s with a hard ceiling of 3600s. Work per refresh is capped
at ten article bodies across all feeds, so a big feed cannot turn one cycle
into a hundred page fetches; the feeds that lost the race are recorded and pick
up next cycle.

Extraction, not headlines. An item only becomes an article once trafilatura
returned a body of at least 200 characters. A headline with no body is not
evidence, so it is dropped rather than stored half-formed.

The merkle_log_entries table does not exist in the dev database yet. The
stamping path is therefore defensive: the first missing-table error disables
stamping for the rest of the run, logs a warning, and leaves content_hash plus
provenance on the result dict so a later step can stamp retroactively. Applying
docs/rss-evidence-merkle-ddl.sql turns it on. pipeline_runs is treated the same
way: a missing ledger table must not sink an ingestion run.

The Batch C columns canonical_url_v1 and url_hash_v1 are deliberately NOT set
here. They are not in the dev database yet, and
scripts/backfill_url_hash_v1.py covers the existing rows. Setting them on
insert only would leave the column half-populated and make the backfill's
"already filled" check lie.

Feeds dropped on purpose, so the absence is not mistaken for an oversight:

  Reuters    skipped: bot-blocked, verified in Batch B.
  AP         skipped: no working RSS, every endpoint 403, verified in Batch B.
  DW         skipped: https://rss.dw.com/rdf/rss-en-all is alive and returns
             134 RDF items (verified 2026-10-01), deliberately left out to keep
             the initial feed set small.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from defusedxml.ElementTree import fromstring as safe_fromstring

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import EdgePredicate, EntityEdge, RawArticle, SourceTier
from src.shared.ledger import stage_run
from src.shared.config import get_settings
from src.transparency.log import SqlAlchemyMerkleLog
from src.utils.ner import extract_entities_top_n
from src.utils.trafilatura_extract import (
    compute_content_hash,
    compute_url_hash,
    extract_article,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- feeds

# All four verified live 2026-10-01: HTTP 200 with real items.
#
#   bbc_world        30 items, server sends NO ETag and NO Last-Modified
#   guardian_world   45 items, server sends an ETag (the only conditional one)
#   npr_news         10 items, no validators
#   france24_en      24 items, no validators
#
# Reuters skipped: bot-blocked, verified in Batch B.
# AP skipped: no working RSS, all endpoints 403, verified in Batch B.
# DW (https://rss.dw.com/rdf/rss-en-all, 134 items RDF, verified alive
# 2026-10-01) deliberately left out to keep the initial set small.
EVIDENCE_FEEDS: list[dict[str, Any]] = [
    {
        "key": "bbc_world",
        "name": "BBC",
        "domain": "bbc.co.uk",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "tier": SourceTier.TIER1,
    },
    {
        "key": "guardian_world",
        "name": "The Guardian",
        "domain": "theguardian.com",
        "url": "https://www.theguardian.com/world/rss",
        "tier": SourceTier.TIER1,
    },
    {
        "key": "npr_news",
        "name": "NPR",
        "domain": "npr.org",
        "url": "https://feeds.npr.org/1001/rss.xml",
        "tier": SourceTier.TIER1,
    },
    {
        "key": "france24_en",
        "name": "France 24",
        "domain": "france24.com",
        "url": "https://www.france24.com/en/rss",
        "tier": SourceTier.TIER1,
    },
]

USER_AGENT = "news-pipeline-evidence/1.0"
ACCEPT = "application/rss+xml, application/xml, text/xml"

HTTP_TIMEOUT_SECONDS = 20

# Cadence. Base is the polite default; the yield case shortens it because a feed
# that just produced items is likely to produce more soon; three 304s in a row
# means nothing is happening and the full hour applies.
POLL_INTERVAL_BASE_SECONDS = 900
POLL_INTERVAL_YIELD_SECONDS = 600
POLL_INTERVAL_IDLE_SECONDS = 3600
CONSECUTIVE_304S_FOR_IDLE = 3

# Backoff. Exponential from 60s, doubling, hard ceiling 3600s. Retry-After is
# honored on 429/503 but never past the same ceiling, so a hostile or buggy
# header cannot park a feed until tomorrow.
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600

# Article bodies fetched per refresh, across all feeds. The cap is on page
# fetches, not feed fetches: a feed can be read as often as it likes, but at
# most this many article bodies are pulled per cycle.
MAX_BODY_FETCHES_PER_REFRESH = 10

# A body shorter than this is a headline, not an article.
MIN_BODY_CHARS = 200

# The state file. Resolved from the repo root (parents[2] of this file) rather
# than the cwd, because a cron job and an interactive run have different cwds
# and the state must be the same file for both. RSS_EVIDENCE_STATE_PATH
# overrides it, which is what the tests use.
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[2] / "var" / "rss_evidence_state.json"

WHITESPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")

# Namespaces we care about. Item extraction is namespace-aware: RDF/RSS 1.0 puts
# items in the RSS 1.0 namespace directly under rdf:RDF, not under a channel.
RSS10_NS = "http://purl.org/rss/1.0/"


def state_path() -> Path:
    """Where FeedState JSON lives. Env override wins, else var/ under the repo root."""
    override = os.getenv("RSS_EVIDENCE_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_STATE_PATH


# ------------------------------------------------------------------ FeedState


@dataclass
class FeedState:
    """Per-feed poller memory: validators, cadence, and failure history.

    This is the poller's memory of what it has already asked for. Persisted as
    JSON so it survives a process restart, which matters because the ETag is
    only useful if the next process also remembers it -- otherwise every restart
    re-downloads every feed.
    """

    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_poll_ts: Optional[float] = None
    last_yield_ts: Optional[float] = None
    consecutive_304s: int = 0
    backoff_until_ts: float = 0.0
    consecutive_failures: int = 0

    # --- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "etag": self.etag,
            "last_modified": self.last_modified,
            "last_poll_ts": self.last_poll_ts,
            "last_yield_ts": self.last_yield_ts,
            "consecutive_304s": self.consecutive_304s,
            "backoff_until_ts": self.backoff_until_ts,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FeedState":
        data = data or {}
        return cls(
            etag=data.get("etag") or None,
            last_modified=data.get("last_modified") or None,
            last_poll_ts=_as_float(data.get("last_poll_ts")),
            last_yield_ts=_as_float(data.get("last_yield_ts")),
            consecutive_304s=int(data.get("consecutive_304s") or 0),
            backoff_until_ts=_as_float(data.get("backoff_until_ts")) or 0.0,
            consecutive_failures=int(data.get("consecutive_failures") or 0),
        )


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def load_feed_state(path: Path | None = None) -> dict[str, FeedState]:
    """Read the whole state file. A missing or corrupt file yields empty state.

    A corrupt state file is not fatal: worst case we re-fetch every feed once
    with no conditional headers, which is what the pipeline did before this file
    existed. Refusing to run would be worse.
    """
    target = Path(path) if path is not None else state_path()
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("RSS evidence state unreadable at %s, starting empty: %s", target, exc)
        return {}
    feeds = raw.get("feeds") if isinstance(raw, dict) else None
    if not isinstance(feeds, dict):
        return {}
    return {key: FeedState.from_dict(value) for key, value in feeds.items()}


def save_feed_state(states: dict[str, FeedState], path: Path | None = None) -> Path:
    """Write the state file, creating var/ if needed. Returns the path written."""
    target = Path(path) if path is not None else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "feeds": {key: state.to_dict() for key, state in states.items()},
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


# ----------------------------------------------------------------- transport


def build_request_headers(state: FeedState | None) -> dict[str, str]:
    """Headers for one feed poll, including conditional validators we hold.

    The validators go out only when we actually have them. Two of the four
    verified feeds send neither ETag nor Last-Modified, so this is mostly a
    no-op for them and the 304 path is exercised by The Guardian.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    if state is None:
        return headers
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified
    return headers


def _retry_after_seconds(raw: Optional[str]) -> Optional[int]:
    """Parse a Retry-After header as delta-seconds, or None if unusable.

    Only the delta-seconds form is honored. The HTTP-date form would need a
    trusted clock to evaluate, and a server asking us to wait until a date it
    chose is not a constraint worth obeying blindly. An unparsable value falls
    through to exponential backoff, which is the safe direction.
    """
    if not raw:
        return None
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def backoff_seconds(consecutive_failures: int) -> int:
    """Exponential backoff, base 60s, doubling, hard ceiling 3600s."""
    if consecutive_failures <= 0:
        return 0
    exponent = min(consecutive_failures - 1, 20)
    return min(BACKOFF_BASE_SECONDS * (2 ** exponent), BACKOFF_MAX_SECONDS)


def fetch_feed_polite(
    feed_url: str,
    state: FeedState | None = None,
) -> tuple[int, bytes, Optional[str], Optional[str], Optional[int]]:
    """One polite GET. Returns (status, body, etag, last_modified, retry_after).

    status is the HTTP status, or 0 when the request never completed (DNS,
    connect, timeout, proxy failure). Body is empty for 304 and for every
    non-200. retry_after is delta-seconds from the header, None when absent or
    unparsable, and the caller decides the cap.

    urllib only, on purpose: this process egresses through a proxy that
    http.client cannot speak to, and urllib is what honors it.
    """
    headers = build_request_headers(state)
    request = Request(feed_url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read() or b""
            return int(resp.status), body, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), None
    except HTTPError as exc:
        # A 304 arrives as HTTPError, not a success: urllib treats any status
        # outside 2xx as an exception. Read it as what it is.
        retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
        etag = exc.headers.get("ETag") if exc.headers else None
        last_modified = exc.headers.get("Last-Modified") if exc.headers else None
        return int(exc.code), b"", etag, last_modified, retry_after
    except (URLError, OSError) as exc:
        logger.warning("RSS evidence fetch failed for %s: %s", feed_url, exc)
        return 0, b"", None, None, None


# --------------------------------------------------------------------- parsing


def _clean(text: Optional[str]) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()


def _strip_html(text: Optional[str]) -> str:
    """Feed descriptions are HTML fragments; keep the words, drop the markup."""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = TAG_RE.sub(" ", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return _clean(text)


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def parse_feed_xml(body: bytes | str) -> list[dict[str, Any]]:
    """Parse RSS 2.0 or RDF/RSS 1.0 into item dicts. Returns [] on unusable XML.

    defusedxml with forbid_dtd, forbid_entities, and forbid_external all on: a
    feed is untrusted input and an XXE payload must expand nothing and reach
    nothing. A DTD is rejected outright rather than parsed, so a billion-laughs
    bomb dies at the door instead of consuming memory.

    Item extraction is namespace-aware in the sense that matters: RSS 2.0 hangs
    items off <channel>, RDF/RSS 1.0 hangs them off <rdf:RDF> directly, and
    items may also be <item rdf:about=...> with no channel at all. Rather than
    branching on the format we look for the channel, then scan its children for
    item -- which covers both, and covers Atom-ish stragglers too.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not body or not body.strip():
        return []

    try:
        root = safe_fromstring(
            body,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except Exception as exc:
        # defusedxml raises DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden
        # and the usual ParseError. All of them mean "this feed is not usable",
        # and none of them mean the process should die.
        logger.warning("RSS evidence feed XML rejected (%s): %s", type(exc).__name__, exc)
        return []

    container = root
    for child in root:
        if _local(child.tag) == "channel":
            container = child
            break

    items = [node for node in container if _local(node.tag) == "item"]
    # A few RDF feeds put items beside the channel rather than inside it.
    if not items and container is not root:
        items = [node for node in root if _local(node.tag) == "item"]

    parsed: list[dict[str, Any]] = []
    for item in items:
        fields = _item_fields(item)
        link = fields.get("link")
        if not link:
            # RDF 1.0 items carry the URL in rdf:about when there is no <link>.
            about = item.attrib.get(f"{{{RSS10_NS}}}about") or item.attrib.get("about")
            link = _clean(about) if about else ""
        if not link:
            continue
        parsed.append(
            {
                "title": fields.get("title") or "",
                "link": link,
                "published_raw": fields.get("date") or "",
                "summary": _strip_html(fields.get("description") or ""),
            }
        )
    return parsed


def _item_fields(item: Any) -> dict[str, str]:
    """Pull title/date/description out of an item, by local name.

    dc:date (Dublin Core) and pubDate (RSS 2.0) are the same fact under two
    names, and content:encoded / description likewise for the body. Matching on
    local name keeps the namespace out of the call site.
    """
    fields: dict[str, str] = {}
    for child in item:
        name = _local(child.tag)
        if name == "item":
            continue
        text = _clean(child.text)
        if not text:
            continue
        if name == "title" and "title" not in fields:
            fields["title"] = text
        elif name == "date" and "date" not in fields:
            fields["date"] = text
        elif name in ("encoded", "description") and "description" not in fields:
            fields["description"] = text
    return fields


def parse_published(raw: str | None) -> Optional[datetime]:
    """Parse a feed date to tz-aware UTC. RFC 822 (pubDate) first, then ISO 8601."""
    value = _clean(raw)
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# -------------------------------------------------------------------- cadence


def should_poll(state: FeedState, now: float) -> bool:
    """Whether this feed is due at `now` (epoch seconds). Pure, no clock, no I/O.

    Two gates, cheapest first. The backoff gate is absolute: a feed that failed
    or was told to wait is not touched again until its window closes, no matter
    how long ago it last succeeded. The cadence gate is relative to the last
    poll, and which interval applies depends on what that poll produced --
    items means come back sooner, three empty 304s in a row means the feed is
    quiet and wait the full hour.

    A feed that has never been polled has no last_poll_ts and is always due.
    """
    if state is None:
        return True
    if now < (state.backoff_until_ts or 0.0):
        return False
    if state.last_poll_ts is None:
        return True

    last = float(state.last_poll_ts)
    if state.consecutive_304s >= CONSECUTIVE_304S_FOR_IDLE:
        interval = POLL_INTERVAL_IDLE_SECONDS
    elif state.last_yield_ts is not None and last >= float(state.last_yield_ts):
        interval = POLL_INTERVAL_YIELD_SECONDS
    else:
        interval = POLL_INTERVAL_BASE_SECONDS
    return (now - last) >= interval


# -------------------------------------------------------------------- refresh


@dataclass
class RefreshReport:
    """What one refresh() did, per feed and in total."""

    feeds_polled: list[str] = field(default_factory=list)
    feeds_skipped: list[str] = field(default_factory=list)
    feeds_deferred: list[str] = field(default_factory=list)
    feeds_failed: list[str] = field(default_factory=list)
    items_seen: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    body_fetches: int = 0


async def refresh(
    states: dict[str, FeedState],
    feeds: Sequence[dict[str, Any]] | None = None,
    *,
    now_fn=datetime.now,
    extract=extract_article,
    max_body_fetches: int = MAX_BODY_FETCHES_PER_REFRESH,
) -> RefreshReport:
    """Poll every due feed, parse it, and extract bodies for the new items.

    Returns a RefreshReport. `states` is mutated in place; the caller persists
    it. `now_fn` and `extract` are injectable so tests drive this without a
    clock and without a network.

    Feed fetches are sequential and body fetches are capped at
    `max_body_fetches` across the whole refresh, so the first feeds in the list
    fill the budget and the rest are recorded as deferred. That ordering is
    arbitrary but stable, which is what makes the cap fair over cycles: every
    refresh moves the queue forward rather than starving the same feeds.
    """
    feed_list = list(feeds if feeds is not None else EVIDENCE_FEEDS)
    report = RefreshReport()
    failed: list[str] = []
    budget = int(max_body_fetches)

    for feed in feed_list:
        key = feed["key"]
        state = states.setdefault(key, FeedState())
        now = now_fn(timezone.utc)
        now_ts = now.timestamp()

        if not should_poll(state, now_ts):
            report.feeds_skipped.append(key)
            continue

        report.feeds_polled.append(key)
        status, body, etag, last_modified, retry_after = fetch_feed_polite(feed["url"], state)

        if status == 304:
            # No change. Validators stand; the poll counts toward the idle run.
            state.consecutive_304s += 1
            state.consecutive_failures = 0
            state.backoff_until_ts = 0.0
            if etag:
                state.etag = etag
            if last_modified:
                state.last_modified = last_modified
            state.last_poll_ts = now_ts
            continue

        if status == 200:
            state.consecutive_failures = 0
            state.backoff_until_ts = 0.0
            state.consecutive_304s = 0
            state.etag = etag or None
            state.last_modified = last_modified or None
            state.last_poll_ts = now_ts
        elif status == 0:
            # Transport failure. Counts as a failure and backs off.
            state.consecutive_failures += 1
            state.last_poll_ts = now_ts
            state.backoff_until_ts = now_ts + backoff_seconds(state.consecutive_failures)
            failed.append(key)
            logger.warning("RSS evidence feed %s: transport failure", key)
            continue
        elif status in (429, 503):
            state.consecutive_failures += 1
            state.last_poll_ts = now_ts
            if retry_after is not None:
                delay = min(retry_after, BACKOFF_MAX_SECONDS)
            else:
                delay = backoff_seconds(state.consecutive_failures)
            state.backoff_until_ts = now_ts + delay
            failed.append(key)
            logger.warning("RSS evidence feed %s: HTTP %s, backing off %ds", key, status, delay)
            continue
        elif status >= 500:
            state.consecutive_failures += 1
            state.last_poll_ts = now_ts
            delay = backoff_seconds(state.consecutive_failures)
            state.backoff_until_ts = now_ts + delay
            failed.append(key)
            logger.warning("RSS evidence feed %s: HTTP %s, backing off %ds", key, status, delay)
            continue
        else:
            # 4xx that is not 429: this feed is broken, not busy. No retry this
            # run and no long backoff -- a 404 tomorrow is still a 404, and a
            # 403 is usually a bot block we should not keep hammering.
            state.consecutive_failures += 1
            state.last_poll_ts = now_ts
            state.backoff_until_ts = now_ts + backoff_seconds(state.consecutive_failures)
            failed.append(key)
            logger.warning("RSS evidence feed %s: HTTP %s, no retry this run", key, status)
            continue

        # 200 from here on.
        entries = parse_feed_xml(body)
        report.items_seen += len(entries)
        if entries:
            state.last_yield_ts = now_ts
        else:
            state.last_yield_ts = None

        if budget <= 0:
            # Feeds past the cap still get counted as polled, but their items
            # wait for a later cycle rather than blowing the body budget.
            report.feeds_deferred.append(key)
            continue

        for entry in entries:
            if budget <= 0:
                report.feeds_deferred.append(key)
                break
            url = entry.get("link")
            if not url or not entry.get("title"):
                continue
            body_text, extracted_title = await extract(url)
            budget -= 1
            report.body_fetches += 1
            if not body_text or len(body_text) < MIN_BODY_CHARS:
                logger.info("RSS evidence: no usable body for %s, skipping", url)
                continue
            report.items.append(
                {
                    "url": url,
                    "title": extracted_title or entry.get("title") or "",
                    "body_text": body_text,
                    "summary": entry.get("summary") or None,
                    "source_domain": feed["domain"],
                    "source_name": feed.get("name") or feed["domain"],
                    "source_key": key,
                    "source_tier": feed["tier"],
                    "published_at": parse_published(entry.get("published_raw")),
                    "body_sha256": compute_content_hash(body_text),
                }
            )

    report.feeds_failed = failed
    return report


# ------------------------------------------------------------------- article


async def build_articles(
    items: Sequence[dict[str, Any]],
    known_url_hashes: Optional[Iterable[str]] = None,
) -> list[RawArticle]:
    """Turn extracted items into RawArticle rows, skipping known and batch dupes.

    url_hash is compute_url_hash(url), which hashes the u1 canonical form, so a
    url_hash match against a GDELT row is canonical URL equality rather than
    string equality of whatever each source happened to publish. Within-batch
    duplicates are dropped here too, because a feed repeating an item across
    its own pages is normal and should cost one row.

    canonical_url_v1 and url_hash_v1 are deliberately left unset: the Batch C
    columns are not in the dev database yet, and
    scripts/backfill_url_hash_v1.py populates them. Setting them here on insert
    only would make the backfill's "already populated" check skip rows that
    disagree with what it would have computed.

    entity extraction runs in a thread because spaCy is CPU-bound and would
    otherwise block the event loop for the length of every article.
    """
    known = set(known_url_hashes or ())
    settings = get_settings()
    seen: set[str] = set()
    articles: list[RawArticle] = []

    for item in items:
        url = item["url"]
        url_hash = compute_url_hash(url)
        if url_hash in known or url_hash in seen:
            continue
        body_text = item["body_text"]
        entities = await asyncio.to_thread(
            extract_entities_top_n, body_text, top_n=settings.top_n_entities
        )
        seen.add(url_hash)
        articles.append(
            RawArticle(
                url=url,
                url_hash=url_hash,
                title=item["title"],
                body_text=body_text,
                summary=item["summary"],
                source_domain=item["source_domain"],
                source_tier=item.get("source_tier") or SourceTier.TIER1,
                published_at=item.get("published_at"),
                entities=entities,
                content_hash=item.get("body_sha256") or compute_content_hash(body_text),
            )
        )
    return articles


# ---------------------------------------------------------------------- sinks


_MISSING_TABLE_MARKERS = (
    "does not exist",  # Postgres, psycopg2, asyncpg
    "undefinedtable",  # Postgres adapter code
    "undefined table",
    "no such table",  # SQLite, which is what the in-memory test DB speaks
)


def _is_missing_table(exc: BaseException) -> bool:
    """True when this is a "relation does not exist" error, not any other fault.

    Checked by type AND text. The type alone is not enough (a ProgrammingError
    is also a syntax error, a permission problem, and a dropped connection) and
    the text alone is dialect-dependent. Both together is what identifies the
    specific, expected, recoverable condition: a table this deployment has not
    migrated yet.

    Both database error classes are accepted because the two environments we
    run in disagree: Postgres raises ProgrammingError, the SQLite test database
    raises OperationalError.
    """
    if not isinstance(exc, (ProgrammingError, OperationalError)):
        return False
    text = str(getattr(exc, "orig", None) or exc).lower()
    return any(marker in text for marker in _MISSING_TABLE_MARKERS)


async def stamp_observations(
    session: AsyncSession,
    articles: Sequence[RawArticle],
    merkle_log: Any | None = None,
) -> dict[str, Any]:
    """Append one observation per new article to the hash-chained log.

    Payload per article::

        {"type": "rss_evidence", "url": ..., "source_domain": ...,
         "fetched_at": <isoformat>, "body_sha256": <content_hash>,
         "title": ...}

    fetched_at sits INSIDE the payload on purpose. The log's leaf hash covers
    the payload only; index and timestamp sit outside it by design, so a caller
    who needs the fetch time to be tamper-evident has to put it in the payload.
    Putting it there means the chain commits to when we read these bytes, not
    merely that we read them.

    DEFENSIVE: merkle_log_entries does not exist in the dev database yet. The
    first missing-table error disables stamping for the remainder of the run
    (the session is unusable after a failed flush), logs a warning, and reports
    table_missing. The content_hash and provenance stay on the returned dict so
    a later step can stamp them once docs/rss-evidence-merkle-ddl.sql is
    applied -- nothing is lost, only deferred.
    """
    log = merkle_log if merkle_log is not None else SqlAlchemyMerkleLog(session)
    # The DB-backed appends run inside a SAVEPOINT. A failed statement in
    # Postgres aborts the whole transaction, so without a savepoint the first
    # missing-table error would poison every later write in the run, including
    # the article rows we just persisted. Rolling back to the savepoint clears
    # the aborted state and leaves the transaction usable.
    use_savepoint = merkle_log is None
    entries: list[tuple[str, str, int]] = []
    stamped = 0
    disabled = False

    for article in articles:
        if disabled:
            break
        if getattr(article, "id", None) is None:
            continue
        payload = {
            "type": "rss_evidence",
            "url": article.url,
            "source_domain": article.source_domain,
            # Inside the payload on purpose: see the docstring.
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "body_sha256": article.content_hash,
            "title": article.title,
        }
        try:
            if use_savepoint:
                async with session.begin_nested():
                    entry = await log.append(payload)
            else:
                entry = await log.append(payload)
        except Exception as exc:
            if not _is_missing_table(exc):
                raise
            disabled = True
            logger.warning(
                "merkle_log_entries is missing; stamping disabled for this run "
                "(%s). content_hash is retained on the result for later stamping.",
                type(exc).__name__,
            )
            break
        stamped += 1
        entries.append((article.url, entry.leaf_hash_hex, entry.index))

    return {
        "stamped": stamped,
        "entries": entries,
        "table_missing": disabled,
        # Kept so a later step can stamp retroactively after the DDL lands.
        "unstamped": [
            {
                "url": a.url,
                "title": a.title,
                "source_domain": a.source_domain,
                "body_sha256": a.content_hash,
            }
            for a in articles
            if getattr(a, "id", None) is not None
        ][stamped:],
    }


async def link_to_gdelt_radar(session: AsyncSession, articles: Sequence[RawArticle]) -> dict[str, Any]:
    """Join each new RSS article to GDELT radar rows by canonical URL.

    The match is url_hash equality, and url_hash is the SHA256 of the u1
    canonical form, so this is a canonical-URL join: GDELT's copy of the link and
    the feed's copy of the link agree after canonicalization, or they are
    different articles. No fuzzy comparison, because a fuzzy comparison here
    would manufacture evidence links that nothing can later audit.

    One SAME_EVENT_AS edge per match at confidence 100: the two rows are the
    same canonical URL, so this is not an inference about the world, it is an
    observation about the bytes. Existing edges are skipped rather than
    duplicated, which makes a re-run idempotent.

    entity_edges exists in dev, so this sink is live. A match whose entities
    carry an EVENT key is recorded separately: that is GDELT's own event
    classification, i.e. radar confirmation rather than a bare URL collision.
    """
    links: list[dict[str, Any]] = []
    gdelt_confirmed = 0
    skipped_existing = 0

    for article in articles:
        if getattr(article, "id", None) is None:
            continue
        rows = await session.execute(
            select(RawArticle).where(RawArticle.url_hash == article.url_hash)
        )
        for other in rows.scalars().all():
            if other.id == article.id:
                continue
            existing = await session.execute(
                select(EntityEdge.id).where(
                    EntityEdge.subject_type == "article",
                    EntityEdge.subject_id == article.id,
                    EntityEdge.predicate == EdgePredicate.SAME_EVENT_AS,
                    EntityEdge.object_type == "article",
                    EntityEdge.object_id == other.id,
                )
            )
            if existing.scalar_one_or_none() is not None:
                skipped_existing += 1
                continue
            session.add(
                EntityEdge(
                    subject_type="article",
                    subject_id=article.id,
                    predicate=EdgePredicate.SAME_EVENT_AS,
                    object_type="article",
                    object_id=other.id,
                    confidence=100,
                )
            )
            entities = other.entities if isinstance(other.entities, dict) else {}
            has_event = bool(entities.get("EVENT"))
            if has_event:
                gdelt_confirmed += 1
            links.append(
                {
                    "rss_article_id": str(article.id),
                    "gdelt_article_id": str(other.id),
                    "url": article.url,
                    "matched_domain": other.source_domain,
                    "gdelt_event_entity": has_event,
                }
            )

    await session.flush()
    return {
        "gdelt_links": len(links),
        "links": links,
        "gdelt_event_confirmed": gdelt_confirmed,
        "skipped_existing": skipped_existing,
    }


# --------------------------------------------------------------- orchestration


async def run_evidence_refresh(
    session: AsyncSession,
    known_url_hashes: Optional[Iterable[str]] = None,
    *,
    feeds: Sequence[dict[str, Any]] | None = None,
    state_store_path: Path | None = None,
    merkle_log: Any | None = None,
    now_fn=datetime.now,
    extract=extract_article,
    max_body_fetches: int = MAX_BODY_FETCHES_PER_REFRESH,
) -> dict[str, Any]:
    """One full evidence refresh: poll, extract, persist, stamp, link.

    Order matters. The rows are added and flushed before stamping so every new
    article has an id to stamp and to hang edges off; the flush inside
    SqlAlchemyMerkleLog.append and the EntityEdge foreign-free UUID columns mean
    no commit boundary is crossed here, so the caller still owns the
    transaction.

    persist, stamp and link run inside ledger.stage_run("ingest_rss_evidence")
    so the run is countable and its drops are attributable. DEFENSIVE:
    pipeline_runs does not exist in dev either, so a missing-table error from
    the ledger is logged and the work continues unwrapped -- losing the ledger
    row matters less than losing the articles.
    """
    states = load_feed_state(state_store_path)
    report = await refresh(
        states,
        feeds=feeds,
        now_fn=now_fn,
        extract=extract,
        max_body_fetches=max_body_fetches,
    )
    try:
        save_feed_state(states, state_store_path)
    except OSError as exc:
        logger.warning("RSS evidence state could not be persisted: %s", exc)

    known = set(known_url_hashes or ())
    if known_url_hashes is None:
        # No caller-supplied knowledge: ask the database what it already has.
        # A missing table here would mean raw_articles is gone, which is a real
        # outage and should raise rather than be swallowed.
        rows = await session.execute(select(RawArticle.url_hash))
        known = {value for value in rows.scalars().all() if value}

    articles = await build_articles(report.items, known)

    result: dict[str, Any] = {
        "feeds_polled": report.feeds_polled,
        "feeds_skipped": report.feeds_skipped,
        "feeds_deferred_by_cap": report.feeds_deferred,
        "feeds_failed": list(report.feeds_failed),
        "items_seen": report.items_seen,
        "articles_new": len(articles),
        "body_fetches": report.body_fetches,
        "gdelt_links": 0,
        "gdelt_event_confirmed": 0,
        "stamped": 0,
        "sinks": {"ledger": "ok", "merkle": "ok", "edges": "ok"},
        "samples": [{"title": a.title, "url": a.url} for a in articles[:5]],
        "unstamped": [],
    }

    async def _do_work() -> dict[str, Any]:
        for article in articles:
            session.add(article)
        # Flush so every new row has an id: the stamp and the edges both need one.
        await session.flush()
        result["articles_persisted"] = len(articles)
        # The ORM objects themselves, for the adapter to return from fetch().
        # Not part of the reported result -- samples/sinks/counts are what a
        # caller logs, and an ORM object in a JSON dump is noise.
        result["_articles"] = articles

        stamp = await stamp_observations(session, articles, merkle_log=merkle_log)
        result["stamped"] = stamp["stamped"]
        result["unstamped"] = stamp["unstamped"]
        if stamp["table_missing"]:
            result["sinks"]["merkle"] = "missing_table"

        links = await link_to_gdelt_radar(session, articles)
        result["gdelt_links"] = links["gdelt_links"]
        result["gdelt_event_confirmed"] = links["gdelt_event_confirmed"]
        result["gdelt_link_detail"] = links["links"]
        return result

    # The ledger is entered through an AsyncExitStack rather than `async with`
    # so that the one recoverable failure -- pipeline_runs not being migrated --
    # can be caught without replaying the whole stage body. Everything inside
    # the ledger still runs in the caller's transaction, so a rollback here
    # takes the ledger row with it, exactly as stage_run documents.
    stack = AsyncExitStack()
    recorder = None
    try:
        recorder = await stack.enter_async_context(
            stage_run(session, stage_name="ingest_rss_evidence")
        )
    except Exception as exc:
        if not _is_missing_table(exc):
            raise
        # pipeline_runs is not migrated in dev. The context manager failed on its
        # own INSERT, so no stage row exists and no stage work has happened.
        # The failed statement also aborted the transaction, so the work runs
        # inside a savepoint to clear that before touching anything else.
        result["sinks"]["ledger"] = "missing_table"
        logger.warning(
            "pipeline_runs is missing; ingest_rss_evidence ran without the ledger (%s)",
            type(exc).__name__,
        )
        async with session.begin_nested():
            await _do_work()
        return result

    try:
        recorder.count_in(report.items_seen)
        recorder.count_out(len(articles))
        recorder.drop("deferred_by_cap", len(report.feeds_deferred))
        recorder.drop("feed_failed", len(report.feeds_failed))
        recorder.drop("deduped", max(0, report.body_fetches - len(articles)))
        await _do_work()
    except Exception:
        # Close the stage row with the error text, then let the error through.
        # stage_run re-raises whatever it is handed, hence the suppression: the
        # raise below is the one that matters.
        with suppress(Exception):
            await stack.__aexit__(*sys.exc_info())
        raise
    await stack.aclose()
    return result