"""Thread-safe ingestion statistics tracking."""

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class IngestStats:
    """Thread-safe counter for ingestion events."""
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _counts: Counter = field(default_factory=Counter, init=False)

    def reset(self) -> None:
        """Reset all counters."""
        with self._lock:
            self._counts.clear()

    def record(self, source: str, event: str, n: int = 1) -> None:
        """Record an event for a source."""
        key = (source, event)
        with self._lock:
            self._counts[key] += n

    def snapshot(self) -> Dict[str, int]:
        """Return JSON-serializable snapshot of counts."""
        with self._lock:
            return {f"{source}.{event}": count for (source, event), count in self._counts.items()}

    def render_markdown(self) -> str:
        """Render as markdown table for GitHub Actions summary."""
        with self._lock:
            if not self._counts:
                return "_No ingestion stats recorded._"

            # Group by source
            by_source = {}
            for (source, event), count in self._counts.items():
                if source not in by_source:
                    by_source[source] = {}
                by_source[source][event] = count

            lines = ["| Source | Event | Count |", "|--------|-------|-------|"]
            for source in sorted(by_source.keys()):
                for event in sorted(by_source[source].keys()):
                    count = by_source[source][event]
                    lines.append(f"| {source} | {event} | {count} |")

            return "\n".join(lines)


# Module-level singleton
STATS = IngestStats()