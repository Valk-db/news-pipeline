"""Stealth-edit and correction tracking: every revision we observed, and what was admitted.

raw_articles holds one mutable snapshot of what a URL served at ingestion. Outlets
edit published articles after that -- sometimes with a correction notice, often with
nothing at all -- and a single snapshot cannot tell you which happened. This module is
the archive's answer: re-fetch an article, hash what came back, and when the hash moves,
write an append-only row describing the change and whether the page acknowledged it.

The vocabulary, and why it is deliberately narrow:

* A *revision* is one observed content change. A page that never changes produces no
  rows; the ingested state (raw_articles.content_hash + body_text) is the implicit
  revision 0. Storing a row per observation instead would fill the free-tier database
  with duplicates of articles nobody edited.
* ACKNOWLEDGED means the new bytes admit to the change: a correction or update notice
  near the top of the article or in a corrections block, or a displayed timestamp newer
  than the previous revision's. STEALTH means the bytes changed and nothing on the page
  says so. These are observations about disclosure, not judgements about the outlet.
* A *correction* is a first-class event, stored in its own table linked to the article
  and to the revision it was found in, with the matched snippet kept verbatim.

Two things this module deliberately does not do:

1. Nothing is ever deleted or updated here. This is an append-only recorder; retention
   policy for revision rows is a separate decision (see src/verification/retention.py,
   which does not know about these tables yet).
2. It does not re-derive old text. The archive keeps hashes and diff summaries, not
   bodies, so a diff is produced only when the previous text is at hand: against
   raw_articles.body_text for the first observed change, or against `previous_content`
   if the caller kept it. A later re-scan of the same article records the classification
   and the hash without a diff, and says so in the outcome reason. Keeping every
   revision's bytes is the object-storage move in GRAND_PLAN.md Phase 1, not this table.

Hashing uses compute_content_hash, the same function ingestion dedup uses, so a
revision's content_hash is comparable with raw_articles.content_hash by construction.

Optionally each revision is appended to the transparency log (src/transparency/log.py)
and the returned entry index stored on the row, which is what makes an edit history
externally checkable. The log arrives as a MerkleLog; nothing here depends on its
storage, and `log_index` stays NULL when no log is supplied.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional, Sequence, Tuple, Union
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import ArticleCorrection, ArticleRevision, RawArticle, StatusLog
from src.utils.trafilatura_extract import compute_content_hash

if TYPE_CHECKING:  # the log is optional at runtime, and this keeps it that way
    from src.transparency.log import MerkleLog

logger = logging.getLogger(__name__)

# Which articles are candidates. The design doc asks for re-fetches at +1h, +6h, +24h
# and +7d; a week of window means a daily cron still re-checks recent articles.
DEFAULT_SCAN_WINDOW_HOURS = 168
# Skip an article already re-checked this recently, so consecutive hourly runs do not
# hammer the same URL. The cadence itself is the schedule's business, not the query's.
DEFAULT_RESCAN_AFTER_HOURS = 6
DEFAULT_SCAN_LIMIT = 50
# How much of the article is treated as "the top" for notice and timestamp detection.
DEFAULT_TOP_PARAGRAPHS = 3
# Diff excerpts are a summary for humans and for the reliability jobs, not a copy of
# the article. 1200 chars is roughly one screen of changes.
DEFAULT_MAX_EXCERPT_CHARS = 1200
DEFAULT_MAX_SNIPPET_CHARS = 200
# Enough to record a page that opened with "Correction: ..." plus a second notice, not
# enough to let one chatty article inflate correction_count.
DEFAULT_MAX_CORRECTION_SIGNALS = 5
DIFF_CONTEXT_LINES = 1
# A "Corrections" heading is short and the notice is the paragraph under it, so a
# heading shorter than this keeps the following paragraph in the stored snippet.
BLOCK_HEADING_MAX_CHARS = 80

FetchText = Callable[[str], Awaitable[Optional[str]]]

CORRECTION_SIGNAL_TOP = "top"
CORRECTION_SIGNAL_BODY = "body"
CORRECTION_SIGNAL_BLOCK = "corrections_block"


def _utc(value: datetime) -> datetime:
    """Normalize to tz-aware UTC. Naive input is read as UTC, which is what SQLite
    hands back for a timezone=True column."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CorrectionPattern:
    """One correction signal: the label stored on the row, and the regex matching it."""

    label: str
    pattern: "re.Pattern"

    @classmethod
    def compile(cls, label: str, pattern: str) -> "CorrectionPattern":
        return cls(label=label, pattern=re.compile(pattern, re.IGNORECASE))


# Signals that mean a correction on their own, wherever they appear. "Corrected" or
# "clarification" mid-article is still a correction; "updated" alone is not, because
# ordinary reporting says "the figures were updated" all the time.
DEFAULT_CORRECTION_PATTERNS: Tuple[CorrectionPattern, ...] = (
    CorrectionPattern.compile("correction", r"\bcorrections?\b"),
    CorrectionPattern.compile("clarification", r"\bclarifications?\b"),
    CorrectionPattern.compile("erratum", r"\berrat(?:um|a)\b"),
    CorrectionPattern.compile("corrected", r"\bcorrected\b"),
    CorrectionPattern.compile("amended", r"\bamended\b"),
    CorrectionPattern.compile("retracted", r"\bretract(?:ed|ion|ions)\b"),
    CorrectionPattern.compile(
        "story-updated",
        r"\b(?:story|article|report|post|headline)\s+(?:was|has\s+been|were)\s+updated\b",
    ),
    CorrectionPattern.compile(
        "we-corrected", r"\bwe\s+(?:have\s+)?(?:now\s+)?(?:corrected|updated|amended)\b"
    ),
)

# Weaker signals, counted only near the top of the article or inside a corrections
# block, where a notice is the natural reading and the false-positive cost is low.
DEFAULT_TOP_CORRECTION_PATTERNS: Tuple[CorrectionPattern, ...] = (
    CorrectionPattern.compile("updated", r"\bupdated\b"),
    CorrectionPattern.compile("update", r"\bupdate\s+to\s+this\s+(?:story|article|report)\b"),
)

# A paragraph that introduces a corrections block. "Corrections", "Editor's note",
# "Updated 4:12pm" -- the heading is part of the notice, and so is what follows it.
DEFAULT_CORRECTIONS_BLOCK_PATTERNS: Tuple[CorrectionPattern, ...] = (
    CorrectionPattern.compile(
        "corrections-block",
        r"^\s*(?:corrections?|clarifications?|errata|editor'?s?\s+notes?|updates?|"
        r"update\s+to\s+this\s+(?:story|article|report))\b",
    ),
)

# "Updated: 2026-09-30T14:03:00Z" and friends. Only the top region is searched and the
# last match wins, because pages that show both "Published" and "Updated" put the newer
# one last. Per-source formats are a long tail, which is why the pattern list is a
# parameter rather than a constant.
DEFAULT_DISPLAYED_TIMESTAMP_PATTERNS: Tuple["re.Pattern", ...] = (
    re.compile(
        r"(?i)\b(?:last\s+)?updated(?:\s+at)?\s*[:\-]?\s*"
        r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?)"
    ),
    re.compile(
        r"(?i)\b(?:last\s+)?updated(?:\s+at)?\s*[:\-]?\s*"
        r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*day,?\s+)?"
        r"(?P<ts>[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}"
        r"(?:,?\s+(?:at\s+)?\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)?)?)"
    ),
    re.compile(
        r"(?i)\b(?:last\s+)?updated(?:\s+at)?\s*[:\-]?\s*"
        r"(?P<ts>\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}"
        r"(?:\s+\d{1,2}:\d{2})?)"
    ),
)

_DISPLAYED_TIMESTAMP_FORMATS = (
    "%B %d, %Y %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%B %d, %Y",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%b %d, %Y",
    "%d %B %Y %H:%M",
    "%d %B %Y",
    "%d %b %Y %H:%M",
    "%d %b %Y",
)


@dataclass(frozen=True)
class DiffSummary:
    """How the new text differs from the previous one, in paragraph terms.

    Paragraphs are compared with internal whitespace collapsed, so re-wrapping the
    same sentence is not an edit. changed_paragraphs counts the non-equal paragraphs on
    both sides: a substitution of one paragraph for another counts as two.
    """

    changed_paragraphs: int
    added_paragraphs: int
    removed_paragraphs: int
    excerpt: str
    truncated: bool

    def describe(self) -> str:
        text = (
            f"{self.changed_paragraphs} paragraphs changed "
            f"(+{self.added_paragraphs}/-{self.removed_paragraphs})"
        )
        return f"{text}, excerpt truncated" if self.truncated else text


@dataclass(frozen=True)
class CorrectionSignal:
    """A correction or update notice found in fetched text."""

    label: str
    location: str  # top, body, or corrections_block
    snippet: str
    paragraph_index: int

    def describe(self) -> str:
        return f"{self.label} ({self.location}): {self.snippet}"


@dataclass
class RevisionOutcome:
    """What one re-fetch of one article produced."""

    article_id: UUID
    url: str
    content_hash: str
    changed: bool
    reason: str
    revision_id: Optional[UUID] = None
    revision_number: Optional[int] = None
    change_kind: Optional[ArticleRevision.ChangeKind] = None
    displayed_at: Optional[datetime] = None
    diff: Optional[DiffSummary] = None
    corrections: Optional[List[CorrectionSignal]] = None
    corrections_truncated: bool = False
    log_index: Optional[int] = None
    details: Optional[List[str]] = None

    def __post_init__(self):
        if self.corrections is None:
            self.corrections = []
        else:
            self.corrections = list(self.corrections)
        if self.details is None:
            self.details = []
        else:
            self.details = list(self.details)

    @property
    def correction_count(self) -> int:
        return len(self.corrections or [])

    def describe(self) -> str:
        """One-line summary for logs and CLI output."""
        if not self.changed:
            return f"{self.url}: {self.reason}"
        kind = self.change_kind.value if self.change_kind else "unknown"
        parts = [
            f"{self.url}: {kind}",
            f"revision {self.revision_number}",
            self.diff.describe() if self.diff else "no diff (previous text unavailable)",
            f"corrections: {self.correction_count}",
        ]
        if self.log_index is not None:
            parts.append(f"log entry #{self.log_index}")
        return ", ".join(parts)


@dataclass
class RevisionScanResult:
    """Rows written by a revision scan run."""

    articles_scanned: int = 0
    articles_unchanged: int = 0
    revisions_created: int = 0
    revisions_acknowledged: int = 0
    revisions_stealth: int = 0
    corrections_recorded: int = 0
    revisions_without_diff: int = 0
    corrections_truncated: int = 0
    fetch_failures: int = 0
    skipped_recent: int = 0
    details: Optional[List[str]] = None

    def __post_init__(self):
        if self.details is None:
            self.details = []
        else:
            self.details = list(self.details)

    @property
    def stealth_rate(self) -> float:
        """Share of observed edits the outlet did not acknowledge. 0.0 with no edits."""
        if not self.revisions_created:
            return 0.0
        return self.revisions_stealth / self.revisions_created

    def summary(self) -> str:
        """One-line summary for logs and CLI output."""
        return (
            f"articles scanned: {self.articles_scanned}, revisions: {self.revisions_created} "
            f"({self.revisions_acknowledged} acknowledged, {self.revisions_stealth} stealth, "
            f"stealth rate {self.stealth_rate:.0%}), corrections: {self.corrections_recorded}, "
            f"unchanged: {self.articles_unchanged}, fetch failures: {self.fetch_failures}"
        )


def split_paragraphs(text: str) -> List[str]:
    """Split into paragraphs with internal whitespace collapsed.

    Blank lines separate paragraphs. Text with no blank lines but several lines is
    split one paragraph per line, because extractor output and pasted feed text both
    hard-wrap and would otherwise compare as a single blob.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    blocks = re.split(r"\n\s*\n", stripped)
    if len(blocks) == 1:
        blocks = stripped.splitlines()
    paragraphs = []
    for block in blocks:
        collapsed = " ".join(block.split())
        if collapsed:
            paragraphs.append(collapsed)
    return paragraphs


def _cap(text: str, limit: int, marker: str) -> Tuple[str, bool]:
    """Trim `text` to `limit` characters, marker included. Reports whether it trimmed."""
    if len(text) <= limit:
        return text, False
    if limit <= len(marker):
        return text[:limit], True
    kept = text[: limit - len(marker)]
    newline = kept.rfind("\n")
    if newline > 0:
        kept = kept[:newline]
    return f"{kept}{marker}", True


def summarize_diff(
    previous_text: str,
    content: str,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> DiffSummary:
    """Build a DiffSummary for `content` against `previous_text`."""
    old = split_paragraphs(previous_text)
    new = split_paragraphs(content)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    changed = added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
        added += j2 - j1
        removed += i2 - i1
    lines = difflib.unified_diff(
        old,
        new,
        fromfile="previous revision",
        tofile="current revision",
        lineterm="",
        n=DIFF_CONTEXT_LINES,
    )
    marker = f"\n... [truncated at {max_excerpt_chars} chars]"
    excerpt, truncated = _cap("\n".join(lines), max_excerpt_chars, marker)
    return DiffSummary(
        changed_paragraphs=changed,
        added_paragraphs=added,
        removed_paragraphs=removed,
        excerpt=excerpt,
        truncated=truncated,
    )


def _first_match(paragraph: str, patterns: Sequence[CorrectionPattern]) -> Optional[CorrectionPattern]:
    for candidate in patterns:
        if candidate.pattern.search(paragraph):
            return candidate
    return None


def _snippet(paragraph: str, following: str, max_chars: int, in_block: bool) -> str:
    """Snippet for one signal, capped. A short block heading keeps the notice body."""
    text = paragraph
    if in_block and following and len(paragraph) < BLOCK_HEADING_MAX_CHARS:
        text = f"{paragraph} | {following}"
    return text[:max_chars]


def _scan_corrections(
    content: str,
    patterns: Optional[Sequence[CorrectionPattern]],
    top_patterns: Optional[Sequence[CorrectionPattern]],
    block_patterns: Optional[Sequence[CorrectionPattern]],
    top_paragraphs: int,
    max_snippet_chars: int,
    max_signals: int,
) -> Tuple[List[CorrectionSignal], bool]:
    """Return (signals, truncated) for `content`."""
    strong = DEFAULT_CORRECTION_PATTERNS if patterns is None else patterns
    weak = DEFAULT_TOP_CORRECTION_PATTERNS if top_patterns is None else top_patterns
    blocks = DEFAULT_CORRECTIONS_BLOCK_PATTERNS if block_patterns is None else block_patterns

    paragraphs = split_paragraphs(content)
    if not paragraphs:
        return [], False

    found: List[CorrectionSignal] = []
    seen = set()
    truncated = False

    def add(signal: CorrectionSignal) -> None:
        nonlocal truncated
        key = (signal.label, signal.paragraph_index)
        if key in seen:
            return
        seen.add(key)
        if len(found) < max_signals:
            found.append(signal)
        else:
            truncated = True

    for index, paragraph in enumerate(paragraphs):
        is_top = index < top_paragraphs
        in_block = _first_match(paragraph, blocks) is not None
        if is_top:
            location = CORRECTION_SIGNAL_TOP
        elif in_block:
            location = CORRECTION_SIGNAL_BLOCK
        else:
            location = CORRECTION_SIGNAL_BODY
        # A corrections block puts the notice under its heading, so keep the paragraph
        # that follows a short heading: the heading alone is not the correction.
        following = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
        snippet = _snippet(paragraph, following, max_snippet_chars, in_block)

        strong_hit = _first_match(paragraph, strong)
        if strong_hit is not None:
            add(
                CorrectionSignal(
                    label=strong_hit.label,
                    location=location,
                    snippet=snippet,
                    paragraph_index=index,
                )
            )
        # Weak signals only count where a notice is the natural reading.
        if is_top or in_block:
            weak_hit = _first_match(paragraph, weak)
            if weak_hit is not None:
                add(
                    CorrectionSignal(
                        label=weak_hit.label,
                        location=location,
                        snippet=snippet,
                        paragraph_index=index,
                    )
                )

    if truncated:
        logger.debug(
            f"Correction detection hit the {max_signals}-signal cap; "
            "correction_count is a lower bound for this revision"
        )
    return found, truncated


def detect_corrections(
    content: str,
    patterns: Optional[Sequence[CorrectionPattern]] = None,
    top_patterns: Optional[Sequence[CorrectionPattern]] = None,
    block_patterns: Optional[Sequence[CorrectionPattern]] = None,
    top_paragraphs: int = DEFAULT_TOP_PARAGRAPHS,
    max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
    max_signals: int = DEFAULT_MAX_CORRECTION_SIGNALS,
) -> List[CorrectionSignal]:
    """Find correction and update notices in fetched article text.

    Signals land in one of three places, and the place is stored with the match because
    it changes what the signal is worth: a notice in the first few paragraphs is a
    disclosure, a notice under a corrections heading is a disclosure, and a strong
    signal deep in the body is a statement about the article rather than a notice about
    the edit. `patterns` defaults to the strong set, `top_patterns` to the weak set that
    only counts in the top region or a corrections block, and all three are parameters
    because per-source vocabulary is a long tail.
    """
    found, _truncated = _scan_corrections(
        content,
        patterns=patterns,
        top_patterns=top_patterns,
        block_patterns=block_patterns,
        top_paragraphs=top_paragraphs,
        max_snippet_chars=max_snippet_chars,
        max_signals=max_signals,
    )
    return found

def _parse_displayed_timestamp(raw: str) -> Optional[datetime]:
    """Parse a displayed timestamp. No offset in the string means UTC."""
    text = re.sub(r"\s+", " ", (raw or "").strip()).strip()
    if not text:
        return None
    iso = re.sub(r"\s+UTC$", "", text, flags=re.IGNORECASE)
    iso = re.sub(r"\s?Z$", "+00:00", iso)
    try:
        return _utc(datetime.fromisoformat(iso))
    except ValueError:
        pass
    # Written forms: "September 30, 2026 at 2:03 p.m." and friends.
    normalized = re.sub(r"\ba\.?m\.?", "am", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\bp\.?m\.?", "pm", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r",?\s+at\s+", " ", normalized, flags=re.IGNORECASE)
    normalized = normalized.strip().strip(".,;:")
    for fmt in _DISPLAYED_TIMESTAMP_FORMATS:
        try:
            return _utc(datetime.strptime(normalized, fmt))
        except ValueError:
            continue
    return None




def extract_displayed_timestamp(
    content: str,
    patterns: Optional[Sequence["re.Pattern"]] = None,
    top_paragraphs: int = DEFAULT_TOP_PARAGRAPHS,
) -> Optional[datetime]:
    """Read the update timestamp the page itself displays, if it shows one.

    Only the top region is searched and the last match wins. Returns None when the page
    shows no parseable timestamp, which classification reads as "no disclosure evidence"
    rather than "no change".
    """
    candidates = DEFAULT_DISPLAYED_TIMESTAMP_PATTERNS if patterns is None else patterns
    head = "\n".join(split_paragraphs(content)[: max(top_paragraphs, 1)])
    for pattern in candidates:
        for match in reversed(list(pattern.finditer(head))):
            parsed = _parse_displayed_timestamp(match.group("ts"))
            if parsed is not None:
                return parsed
    return None


async def get_article(
    session: AsyncSession,
    article_id: Optional[Union[UUID, str]] = None,
    url: Optional[str] = None,
) -> Optional[RawArticle]:
    """Look an article up by id or by url, whichever is given."""
    if article_id is not None:
        stmt = select(RawArticle).where(RawArticle.id == article_id)
    elif url is not None:
        stmt = select(RawArticle).where(RawArticle.url == url)
    else:
        raise ValueError("get_article needs an article_id or a url")
    rows = await session.execute(stmt)
    return rows.scalars().first()


async def _resolve_article(
    session: AsyncSession,
    article: Union[RawArticle, UUID, str],
) -> RawArticle:
    """Accept a loaded article, an article id, or a url."""
    if isinstance(article, RawArticle):
        return article
    if isinstance(article, UUID):
        found = await get_article(session, article_id=article)
    else:
        try:
            found = await get_article(session, article_id=UUID(str(article)))
        except ValueError:
            found = await get_article(session, url=str(article))
    if found is None:
        raise ValueError(f"no raw_articles row for {article!r}")
    return found


async def latest_revision(session: AsyncSession, article_id: UUID) -> Optional[ArticleRevision]:
    """The newest revision row for an article, or None if it has never changed."""
    stmt = (
        select(ArticleRevision)
        .where(ArticleRevision.article_id == article_id)
        .order_by(ArticleRevision.revision_number.desc())
        .limit(1)
    )
    rows = await session.execute(stmt)
    return rows.scalars().first()


async def record_revision(
    session: AsyncSession,
    article: Union[RawArticle, UUID, str],
    content: str,
    previous_content: Optional[str] = None,
    fetched_at: Optional[datetime] = None,
    displayed_at: Optional[datetime] = None,
    patterns: Optional[Sequence[CorrectionPattern]] = None,
    top_patterns: Optional[Sequence[CorrectionPattern]] = None,
    block_patterns: Optional[Sequence[CorrectionPattern]] = None,
    timestamp_patterns: Optional[Sequence["re.Pattern"]] = None,
    top_paragraphs: int = DEFAULT_TOP_PARAGRAPHS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
    max_signals: int = DEFAULT_MAX_CORRECTION_SIGNALS,
    log: Optional["MerkleLog"] = None,
    dry_run: bool = False,
) -> RevisionOutcome:
    """Compare freshly fetched content against the latest known state of an article.

    Args:
        session: async session; nothing is committed here
        article: a RawArticle, an article id, or a url to look up
        content: the freshly fetched body text
        previous_content: text to diff against. None falls back to raw_articles.body_text
            when this is the article's first observed change; "" means "no previous text
            available", which records the classification without a diff.
        fetched_at: when we re-fetched (defaults to now)
        displayed_at: the timestamp the page displayed; None means read it from content
        patterns/top_patterns/block_patterns: correction signal sets
        timestamp_patterns: displayed-timestamp regexes
        top_paragraphs: how many leading paragraphs count as "the top"
        max_excerpt_chars: cap on the stored diff excerpt
        max_snippet_chars: cap on a stored correction snippet
        max_signals: cap on correction signals per revision
        log: optional MerkleLog to append the revision observation to
        dry_run: classify and report, write nothing

    Returns a RevisionOutcome. A row is written only when the content hash moved.
    """
    row = await _resolve_article(session, article)
    now = _utc(fetched_at or datetime.now(timezone.utc))

    if not (content or "").strip():
        logger.info(f"{row.url}: empty extraction, no revision recorded")
        return RevisionOutcome(
            article_id=row.id,
            url=row.url,
            content_hash="",
            changed=False,
            reason="empty_content",
            details=["Extraction returned no text; treated as no observation"],
        )

    new_hash = compute_content_hash(content)
    previous = await latest_revision(session, row.id)
    # The ingested state is the implicit revision 0, so an article whose page has not
    # changed since ingestion produces no row at all.
    baseline_hash = previous.content_hash if previous else (row.content_hash or None)
    if baseline_hash is not None and new_hash == baseline_hash:
        logger.debug(f"{row.url}: unchanged ({new_hash[:12]})")
        return RevisionOutcome(
            article_id=row.id,
            url=row.url,
            content_hash=new_hash,
            changed=False,
            reason="unchanged",
        )

    shown = displayed_at
    if shown is None:
        shown = extract_displayed_timestamp(
            content, patterns=timestamp_patterns, top_paragraphs=top_paragraphs
        )
    previous_shown = previous.displayed_at if previous else None
    timestamp_advanced = (
        shown is not None and previous_shown is not None and _utc(shown) > _utc(previous_shown)
    )
    corrections, corrections_truncated = _scan_corrections(
        content,
        patterns=patterns,
        top_patterns=top_patterns,
        block_patterns=block_patterns,
        top_paragraphs=top_paragraphs,
        max_snippet_chars=max_snippet_chars,
        max_signals=max_signals,
    )

    if corrections:
        change_kind = ArticleRevision.ChangeKind.ACKNOWLEDGED
        reason = "correction_notice"
    elif timestamp_advanced:
        change_kind = ArticleRevision.ChangeKind.ACKNOWLEDGED
        reason = "displayed_timestamp"
    else:
        change_kind = ArticleRevision.ChangeKind.STEALTH
        reason = "silent_change"

    if previous_content is None and previous is None:
        baseline_text = row.body_text
    else:
        baseline_text = previous_content
    diff = summarize_diff(baseline_text, content, max_excerpt_chars) if baseline_text else None
    if diff is None:
        reason = f"{reason}_without_previous_text"
        logger.info(
            f"{row.url}: no previous text on hand, recording "
            f"{change_kind.value} without a diff summary"
        )

    outcome = RevisionOutcome(
        article_id=row.id,
        url=row.url,
        content_hash=new_hash,
        changed=True,
        reason=reason,
        revision_number=(previous.revision_number + 1) if previous else 1,
        change_kind=change_kind,
        displayed_at=shown,
        diff=diff,
        corrections=corrections,
        corrections_truncated=corrections_truncated,
    )

    if dry_run:
        outcome.details.append("Dry run: no revision or correction row written")
        return outcome

    revision = ArticleRevision(
        article_id=row.id,
        revision_number=outcome.revision_number,
        previous_revision_id=previous.id if previous else None,
        content_hash=new_hash,
        fetched_at=now,
        displayed_at=shown,
        change_kind=change_kind,
        changed_paragraphs=diff.changed_paragraphs if diff else 0,
        diff_excerpt=diff.excerpt if diff else None,
        diff_truncated=diff.truncated if diff else False,
        correction_count=len(corrections),
    )
    session.add(revision)
    # The id is a Python-side default, so it does not exist until the insert.
    await session.flush()
    outcome.revision_id = revision.id

    for signal in corrections:
        session.add(
            ArticleCorrection(
                article_id=row.id,
                revision_id=revision.id,
                signal=signal.label,
                location=signal.location,
                snippet=signal.snippet,
                detected_at=now,
            )
        )

    if log is not None:
        entry = await log.append(
            {
                "type": "article_revision",
                "article_id": str(row.id),
                "url": row.url,
                "source_domain": row.source_domain,
                "revision_number": outcome.revision_number,
                "content_hash": new_hash,
                "previous_content_hash": baseline_hash or "",
                # Inside the payload on purpose: the log deliberately does not vouch
                # for its own timestamp column.
                "fetched_at": now.isoformat(),
                "change_kind": change_kind.value,
                "changed_paragraphs": diff.changed_paragraphs if diff else 0,
                "corrections": [signal.label for signal in corrections],
            }
        )
        revision.log_index = entry.index
        outcome.log_index = entry.index

    await session.flush()
    return outcome


async def default_fetch_text(url: str) -> Optional[str]:
    """Re-fetch a URL and extract its body with the extractor ingestion uses.

    Imported lazily so that a dry run, a test with an injected fetcher, or a caller
    holding its own HTTP path never pulls in the network stack.
    """
    from src.utils.trafilatura_extract import extract_article

    body, _title = await extract_article(url)
    return body


async def scan_revisions(
    session: AsyncSession,
    window_hours: int = DEFAULT_SCAN_WINDOW_HOURS,
    rescan_after_hours: int = DEFAULT_RESCAN_AFTER_HOURS,
    limit: int = DEFAULT_SCAN_LIMIT,
    fetch_text: Optional[FetchText] = None,
    patterns: Optional[Sequence[CorrectionPattern]] = None,
    top_patterns: Optional[Sequence[CorrectionPattern]] = None,
    block_patterns: Optional[Sequence[CorrectionPattern]] = None,
    timestamp_patterns: Optional[Sequence["re.Pattern"]] = None,
    top_paragraphs: int = DEFAULT_TOP_PARAGRAPHS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    max_snippet_chars: int = DEFAULT_MAX_SNIPPET_CHARS,
    max_signals: int = DEFAULT_MAX_CORRECTION_SIGNALS,
    log: Optional["MerkleLog"] = None,
    dry_run: bool = False,
    write_status_log: bool = True,
    commit: bool = True,
) -> RevisionScanResult:
    """Re-fetch recently ingested articles and record every revision found.

    Args:
        session: async session (caller owns it; commits unless commit=False)
        window_hours: only articles ingested within this window are candidates
        rescan_after_hours: skip an article checked this recently (0 disables)
        limit: maximum candidate articles per run
        fetch_text: coroutine taking a url and returning body text or None
        patterns/top_patterns/block_patterns: correction signal sets
        timestamp_patterns: displayed-timestamp regexes
        top_paragraphs: how many leading paragraphs count as "the top"
        max_excerpt_chars: cap on each stored diff excerpt
        max_snippet_chars: cap on each stored correction snippet
        max_signals: cap on correction signals per revision
        log: optional MerkleLog to append revision observations to
        dry_run: re-fetch and classify, write nothing (the fetches still happen)
        write_status_log: append a StatusLog row for the run
        commit: commit at the end (False leaves it to the caller)

    Returns a RevisionScanResult. Inserts only: no row is updated or deleted.
    """
    if window_hours <= 0:
        raise ValueError(
            f"window_hours must be a positive number of hours (got {window_hours}); "
            "refusing to scan revisions over an empty window"
        )
    if rescan_after_hours < 0:
        raise ValueError(f"rescan_after_hours must be zero or more (got {rescan_after_hours})")
    if limit <= 0:
        raise ValueError(f"limit must be a positive number of articles (got {limit})")

    fetcher = default_fetch_text if fetch_text is None else fetch_text
    result = RevisionScanResult()
    now = _utc(datetime.now(timezone.utc))
    cutoff = now - timedelta(hours=window_hours)

    logger.info(
        f"Revision scan starting: window_hours={window_hours} "
        f"(cutoff {cutoff.isoformat()}), rescan_after_hours={rescan_after_hours}, "
        f"limit={limit}, dry_run={dry_run}"
    )

    stmt = (
        select(RawArticle)
        .where(RawArticle.fetched_at >= cutoff)
        .order_by(RawArticle.fetched_at.desc())
        .limit(limit)
    )
    articles = (await session.execute(stmt)).scalars().all()
    logger.info(
        f"Revision scan candidates: {len(articles)} articles ingested since {cutoff.date()}"
    )

    for article in articles:
        if rescan_after_hours:
            previous = await latest_revision(session, article.id)
            if previous is not None:
                age = now - _utc(previous.fetched_at)
                if age < timedelta(hours=rescan_after_hours):
                    hours_since = age.total_seconds() / 3600
                    result.skipped_recent += 1
                    logger.info(
                        f"{article.url}: checked {hours_since:.1f}h ago, inside "
                        f"rescan_after_hours={rescan_after_hours}, skipping"
                    )
                    result.details.append(
                        f"Skipped {article.url}: last checked {hours_since:.1f}h ago"
                    )
                    continue

        result.articles_scanned += 1
        try:
            content = await fetcher(article.url)
        except Exception as exc:  # one bad URL must not end the batch
            result.fetch_failures += 1
            logger.warning(f"{article.url}: fetch raised {type(exc).__name__}")
            result.details.append(f"Fetch failed for {article.url}: {type(exc).__name__}")
            continue

        if not (content or "").strip():
            # No text back is a failed observation, not an unchanged article: counting
            # it as unchanged would quietly inflate the "nothing changed" side.
            result.fetch_failures += 1
            logger.info(f"{article.url}: no text extracted, no revision recorded")
            result.details.append(f"No text extracted from {article.url}")
            continue

        outcome = await record_revision(
            session,
            article,
            content,
            previous_content=article.body_text,
            fetched_at=now,
            patterns=patterns,
            top_patterns=top_patterns,
            block_patterns=block_patterns,
            timestamp_patterns=timestamp_patterns,
            top_paragraphs=top_paragraphs,
            max_excerpt_chars=max_excerpt_chars,
            max_snippet_chars=max_snippet_chars,
            max_signals=max_signals,
            log=log,
            dry_run=dry_run,
        )
        result.details.extend(outcome.details or [])

        if not outcome.changed:
            result.articles_unchanged += 1
            logger.info(f"{article.url}: {outcome.reason}")
            continue

        result.revisions_created += 1
        if outcome.change_kind == ArticleRevision.ChangeKind.ACKNOWLEDGED:
            result.revisions_acknowledged += 1
        elif outcome.change_kind == ArticleRevision.ChangeKind.STEALTH:
            result.revisions_stealth += 1
        result.corrections_recorded += outcome.correction_count
        if outcome.diff is None:
            result.revisions_without_diff += 1
        if outcome.corrections_truncated:
            result.corrections_truncated += 1
        logger.info(outcome.describe())
        result.details.append(outcome.describe())

    logger.info(f"Revision scan complete{' (dry run)' if dry_run else ''}: {result.summary()}")

    if write_status_log and not dry_run:
        await session.execute(
            StatusLog.__table__.insert().values(
                phase="revision_scan",
                status="ok",
                details={
                    "window_hours": window_hours,
                    "rescan_after_hours": rescan_after_hours,
                    "limit": limit,
                    "articles_scanned": result.articles_scanned,
                    "articles_unchanged": result.articles_unchanged,
                    "revisions_created": result.revisions_created,
                    "revisions_acknowledged": result.revisions_acknowledged,
                    "revisions_stealth": result.revisions_stealth,
                    "corrections_recorded": result.corrections_recorded,
                    "fetch_failures": result.fetch_failures,
                    "skipped_recent": result.skipped_recent,
                },
            )
        )

    if commit and not dry_run:
        await session.commit()
    return result
