#!/usr/bin/env python3
"""Pull the pipeline's ingested articles for the comparison window from dev DB.

Window: fetched_at >= 2026-10-01 18:00 UTC (all rows the dev DB holds for the
comparison period; max fetched_at is 2026-10-02 ~19:54 UTC).
Read-only SELECTs against the dev project only.
"""
import json
import os
import re
import sys

sys.path.insert(0, "/home/hatch/workspace/wt-blindspot")

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                   "pipeline_articles.jsonl")


def main() -> int:
    import psycopg
    env = {}
    with open(os.path.expanduser("~/.config/procmon/supabase-dev.env")) as f:
        for line in f:
            m = re.match(r"\s*([A-Z_]+)=(.*)", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    conn = psycopg.connect(host="127.0.0.1", port=15432, user="postgres",
                           dbname="postgres",
                           password=env["SUPABASE_DB_PASSWORD"])
    with conn.cursor() as cur:
        cur.execute("""
            select id::text, url, title, source_domain, source_tier,
                   published_at::text, fetched_at::text,
                   entities, detected_language, terminal_state
            from raw_articles
            where fetched_at >= '2026-10-01T18:00:00Z'
            order by fetched_at
        """)
        rows = cur.fetchall()
        cur.execute("select count(*) from stories where created_at >= '2026-10-01T18:00:00Z'")
        n_stories = cur.fetchone()[0]
        cur.execute("select event_type::text, count(*) from events "
                    "where created_at >= '2026-10-01T18:00:00Z' group by 1")
        events_by_type = cur.fetchall()
    conn.close()

    n = 0
    with open(OUT, "w", encoding="utf-8") as f:
        for (aid, url, title, domain, tier, pub, fetched, entities,
             lang, tstate) in rows:
            f.write(json.dumps({
                "id": aid, "url": url, "title": title,
                "source_domain": domain, "source_tier": str(tier),
                "published_at": pub, "fetched_at": fetched,
                "entities": entities, "detected_language": lang,
                "terminal_state": tstate,
            }, ensure_ascii=False, default=str) + "\n")
            n += 1
    print(f"articles={n} stories_in_window={n_stories} "
          f"events_by_type={events_by_type}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
