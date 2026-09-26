"""Common contract every ingestion source implements.

Nothing in an adapter fetches on construction -- only when `fetch()` is
called by the orchestrator in run.py. This mirrors the source_registry.py
convention of declaring config without doing network I/O at import time.
"""
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from src.schema.models import RawArticle


@dataclass
class SourceHealth:
    """Health snapshot returned by a source adapter's health_check().

    status is one of "ok" | "degraded" | "down". succeeded/failed/skipped
    hold per-domain or per-feed identifiers -- gdelt.py's existing health
    dict (succeeded/failed/skipped keys) maps directly onto this shape.
    """
    status: str
    detail: str = ""
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@runtime_checkable
class SourceAdapter(Protocol):
    """Every ingestion source (RSS, GDELT, Reddit, and future sensor/OSINT/
    gov-doc sources) implements this."""

    name: str  # e.g. "rss_tier1", "rss_tier2", "gdelt", "reddit_tier3"

    async def fetch(self) -> list[RawArticle]:
        """Fetch and return new articles. Must not raise on a single feed's
        failure inside the batch -- log/record it via IngestStats and
        continue; only raise for a total adapter failure (e.g. the adapter
        cannot reach the network at all)."""
        ...

    async def health_check(self) -> SourceHealth:
        """Cheap status check. RSS/GDELT-style adapters should reuse the
        per-domain result from the most recent fetch() rather than making a
        fresh network call here."""
        ...