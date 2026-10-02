"""Report which configured feeds are producing nothing, and for how long.

Reads the same state the ingestion run writes (var/feed_health.json, or
FEED_HEALTH_STATE_PATH) and prints the verdict per feed URL. A feed is DEAD
after three consecutive polls that yielded no entries; the point of this script
is that you can ask the question without waiting for a run to print it.

Exit status is 1 when any feed is dead, so a scheduler or CI step can fail on a
silently rotting feed. The report never edits the registry: a dead verdict is
something a human should act on, not something the monitor should quietly fix.
"""

import argparse
import json
import sys

from src.ingestion.feed_health import (
    DEAD_AFTER_EMPTY_POLLS,
    load_registry,
    render_health_report,
    report_dead_feeds,
    state_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-path",
        default=None,
        help="Feed health state file (default: $FEED_HEALTH_STATE_PATH or var/feed_health.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the dead feeds as JSON instead of a markdown table",
    )
    args = parser.parse_args(argv)

    registry = load_registry(args.state_path)

    if args.json:
        print(json.dumps(report_dead_feeds(registry), indent=2))
        return 1 if registry.dead() else 0

    print(f"Feed health state: {state_path(args.state_path)}")
    print(f"Feeds tracked: {len(registry.feeds)}")
    print()
    print(render_health_report(registry))

    dead = registry.dead()
    if dead:
        print()
        print(
            f"{len(dead)} feed(s) DEAD: no entries in {DEAD_AFTER_EMPTY_POLLS} "
            "consecutive polls. Each is a coverage hole in the registry."
        )
        return 1

    print()
    print("No dead feeds. (Feeds marked 'empty' have yielded nothing since their last run.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
