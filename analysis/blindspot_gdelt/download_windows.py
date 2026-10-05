#!/usr/bin/env python3
"""Download GDELT 2.0 15-minute event files for a fixed 24h window.

Window: 2026-10-01 18:00:00 UTC .. 2026-10-02 18:00:00 UTC (96 quarter-hours).
Only the .export.CSV.zip (event rows) is fetched; mentions/GKG are not needed
for the region/theme census. A 404 on a window is a soft skip (file rotated).

Uses curl (not urllib): this VM's egress proxy truncates large urllib
responses with IncompleteRead, while curl completes them (AGENTS.md lesson).
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, UTC

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BASE = "http://data.gdeltproject.org/gdeltv2/"
START = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
WINDOWS = 96  # quarter-hours


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M") + "00"


def main() -> int:
    os.makedirs(DATA, exist_ok=True)
    ok, skipped, failed = 0, 0, 0
    for i in range(WINDOWS):
        ts = stamp(START + timedelta(minutes=15 * i))
        dest = os.path.join(DATA, f"{ts}.export.CSV.zip")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            ok += 1
            continue
        url = BASE + ts + ".export.CSV.zip"
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "60", "--retry", "2",
             "-o", dest, "-w", "%{http_code}", url],
            capture_output=True, text=True)
        code = (r.stdout or "").strip()[-3:]
        size = os.path.getsize(dest) if os.path.exists(dest) else 0
        if code == "200" and size > 0:
            ok += 1
        elif code == "404":
            skipped += 1
            if os.path.exists(dest):
                os.remove(dest)
        else:
            failed += 1
            print(f"WARN {ts}: http={code} size={size} stderr={r.stderr[:120]}",
                  file=sys.stderr)
            if os.path.exists(dest):
                os.remove(dest)
        if i % 24 == 23:
            print(f"progress {i+1}/{WINDOWS} ok={ok} skipped404={skipped} failed={failed}",
                  flush=True)
    print(f"DONE ok={ok} skipped404={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
