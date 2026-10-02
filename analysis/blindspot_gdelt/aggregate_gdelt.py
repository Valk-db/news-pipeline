#!/usr/bin/env python3
"""Aggregate GDELT 2.0 15-minute event files into a deduplicated event census.

Reads data/<ts>.export.CSV.zip for the 24h window, parses with the repo's own
column map (src.ingestion.gdelt_static.EV) and TSV reader, dedupes by
GlobalEventID (keeping the latest DateAdded), and writes one JSON row per
unique event to data/gdelt_events.jsonl plus a parse report.

Usage: python aggregate_gdelt.py
"""
import glob
import json
import os
import sys

sys.path.insert(0, "/home/hatch/workspace/wt-blindspot")
from src.ingestion.gdelt_static import EV, EV_MIN_COLUMNS, iter_tsv_rows  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

ROOT_FAMILY = {
    "01": "VERBAL", "02": "VERBAL",
    "03": "COOPERATION", "04": "COOPERATION", "05": "COOPERATION",
    "06": "COOPERATION", "08": "COOPERATION",
    "07": "AID",
    "09": "INVESTIGATE",
    "10": "DISAPPROVE", "11": "DISAPPROVE", "12": "DISAPPROVE",
    "13": "THREAT",
    "14": "PROTEST",
    "15": "COERCION", "16": "COERCION", "17": "COERCION",
    "18": "VIOLENCE",
    "19": "ARMED_CONFLICT",
    "20": "MASS_VIOLENCE",
}

ROOT_LABEL = {
    "01": "Make public statement", "02": "Appeal",
    "03": "Express intent to cooperate", "04": "Consult",
    "05": "Engage in diplomatic cooperation", "06": "Engage in material cooperation",
    "07": "Provide aid", "08": "Yield", "09": "Investigate", "10": "Demand",
    "11": "Disapprove", "12": "Reject", "13": "Threaten", "14": "Protest",
    "15": "Exhibit force posture", "16": "Reduce relations", "17": "Coerce",
    "18": "Assault", "19": "Fight", "20": "Unconventional mass violence",
}


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    files = sorted(glob.glob(os.path.join(DATA, "*.export.CSV.zip")))
    print(f"parsing {len(files)} window files", flush=True)
    events = {}  # GlobalEventID -> row dict (dedupe: latest DateAdded wins)
    rows_seen = 0
    malformed = 0
    for i, path in enumerate(files):
        for row in iter_tsv_rows(path):
            if len(row) < EV_MIN_COLUMNS:
                malformed += 1
                continue
            rows_seen += 1
            gid = row[EV["global_event_id"]].strip()
            root = row[EV["event_root_code"]].strip()
            rec = {
                "event_id": gid,
                "sqldate": row[EV["sqldate"]].strip(),
                "date_added": row[EV["date_added"]].strip(),
                "event_code": row[EV["event_code"]].strip(),
                "event_root": root,
                "family": ROOT_FAMILY.get(root, "UNKNOWN"),
                "root_label": ROOT_LABEL.get(root, "unknown"),
                "quad_class": row[EV["quad_class"]].strip(),
                "goldstein": to_float(row[EV["goldstein_scale"]]),
                "num_mentions": to_int(row[EV["num_mentions"]]),
                "num_sources": to_int(row[EV["num_sources"]]),
                "num_articles": to_int(row[EV["num_articles"]]),
                "avg_tone": to_float(row[EV["avg_tone"]]),
                "geo_name": row[EV["action_geo_name"]].strip(),
                "geo_country_fips": row[EV["action_geo_country"]].strip(),
                "source_url": row[EV["source_url"]].strip(),
            }
            prev = events.get(gid)
            if prev is None or rec["date_added"] >= prev["date_added"]:
                # tie/refresh: keep the row with more articles on equal dates
                if prev is None or rec["date_added"] > prev["date_added"] or \
                        rec["num_articles"] >= prev["num_articles"]:
                    events[gid] = rec
        if i % 24 == 23:
            print(f"  {i+1}/{len(files)} unique_events={len(events)}", flush=True)

    out = os.path.join(DATA, "gdelt_events.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for rec in events.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    report = {
        "window_files": len(files),
        "rows_seen": rows_seen,
        "malformed_rows": malformed,
        "unique_events": len(events),
    }
    json.dump(report, open(os.path.join(DATA, "gdelt_parse_report.json"), "w"), indent=1)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
