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
        """Render as markdown table for GitHub Actions summary.

        Shows fetch_failed:* and feed_failed:* as separate columns (or clearly labeled rows).
        """
        with self._lock:
            if not self._counts:
                return "_No ingestion stats recorded._"

            # Group by source
            by_source = {}
            for (source, event), count in self._counts.items():
                if source not in by_source:
                    by_source[source] = {}
                by_source[source][event] = count

            # Define event order for consistent columns
            event_order = [
                "entries_in_feed",
                "entries_seen",
                "already_known",
                "ok",
                "too_short",
                "circuit_open",
                "circuit_tripped",
                "feed_ok",
            ]
            # Add any feed_failed:* events dynamically
            feed_failed_events = []
            for source_events in by_source.values():
                for event in source_events.keys():
                    if event.startswith("feed_failed:") and event not in feed_failed_events:
                        feed_failed_events.append(event)
            feed_failed_events.sort()

            event_order.extend(feed_failed_events)

            # Add any remaining events not in order
            all_events = set()
            for source_events in by_source.values():
                all_events.update(source_events.keys())
            for event in sorted(all_events):
                if event not in event_order:
                    event_order.append(event)

            # Render table
            header = "| Source | " + " | ".join(event_order) + " |"
            sep = "|--------|" + "|".join(["-------|" for _ in event_order])
            lines = [header, sep]

            for source in sorted(by_source.keys()):
                row = [source]
                for event in event_order:
                    count = by_source[source].get(event, 0)
                    row.append(str(count) if count > 0 else "—")
                lines.append("| " + " | ".join(row) + " |")

            return "\n".join(lines)


# Module-level singleton
STATS = IngestStats()