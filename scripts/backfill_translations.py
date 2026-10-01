"""Backfill English translations for existing raw_articles via Supabase REST.

Direct Postgres TCP is blocked from this VM, so this goes through postgREST.
Requires the translation migration to be applied first:
    supabase/migrations/20261001000400_translation_v1.sql
(via the dashboard SQL editor; the script checks and refuses otherwise).

Usage:
    python scripts/backfill_translations.py [--limit N] [--dry-run]

Reads SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from
~/.config/procmon/supabase-dev.env. Polite by construction (MyMemory backend:
1s between calls, daily char budget).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.enrichment.translation import (
    detect_language,
    select_backend,
    translate_articles,
)


def load_env() -> dict:
    env: dict[str, str] = {}
    path = Path.home() / ".config" / "procmon" / "supabase-dev.env"
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Rest:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.key = key

    def _req(self, path: str, method: str = "GET", body: dict | None = None):
        # curl subprocess: the egress proxy mangles chunked urllib responses
        # from Supabase; curl handles them correctly.
        import subprocess

        headers = [
            "-H", f"apikey: {self.key}",
            "-H", f"Authorization: Bearer {self.key}",
            "-H", "Content-Type: application/json",
        ]
        cmd = ["curl", "-s", "--max-time", "60", "-X", method] + headers
        if body is not None:
            cmd += ["-d", json.dumps(body)]
            if method == "PATCH":
                cmd += ["-H", "Prefer: return=representation"]
        cmd.append(f"{self.url}/rest/v1/{path}")
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
        if out.returncode != 0:
            raise RuntimeError(f"curl failed: {out.stderr[:200]}")
        try:
            return json.loads(out.stdout) if out.stdout.strip() else []
        except json.JSONDecodeError:
            raise RuntimeError(f"bad JSON from REST: {out.stdout[:200]}")

    def get(self, path: str):
        return self._req(path)

    def patch(self, path: str, body: dict):
        return self._req(path, method="PATCH", body=body)


class ArticleProxy:
    """Duck-typed RawArticle for the translation module."""

    def __init__(self, row: dict):
        self.id = row["id"]
        self.url = row.get("url")
        self.title = row.get("title")
        self.body_text = row.get("body_text")
        self.detected_language = None
        self.title_en = None
        self.body_text_en = None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    rest = Rest(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])

    # Check the migration is applied before touching anything.
    probe = rest.get("raw_articles?select=id&limit=1")
    _ = probe  # noqa: F841 (connection check)
    cols = rest.get("raw_articles?select=detected_language&limit=1")
    if isinstance(cols, dict) and cols.get("message"):
        print("ABORT: translation columns missing in dev. Apply")
        print("  supabase/migrations/20261001000400_translation_v1.sql")
        print("via the dashboard SQL editor first.")
        return 2

    rows = rest.get(
        "raw_articles?select=id,url,title,body_text"
        "&detected_language=is.null"
        f"&order=id&limit={args.limit}"
    )
    print(f"untranslated articles: {len(rows)}")
    if not rows:
        return 0

    proxies = [ArticleProxy(r) for r in rows]
    backend = select_backend()
    print(f"backend: {backend.name}")
    summary = translate_articles(proxies, backend=backend)
    print(f"summary: {summary}")

    if args.dry_run:
        for p in proxies[:5]:
            print(f"  [{p.detected_language}] {(p.title or '')[:60]}")
            if p.title_en:
                print(f"    -> {p.title_en[:80]}")
        print("(dry run, nothing written)")
        return 0

    updated = 0
    for p in proxies:
        body = {
            "detected_language": p.detected_language,
            "title_en": p.title_en,
            "body_text_en": p.body_text_en,
        }
        # Only send non-null fields so English rows just get their language tag.
        body = {k: v for k, v in body.items() if v is not None}
        if not body:
            continue
        qid = urllib.parse.quote(p.id, safe="")
        rest.patch(f"raw_articles?id=eq.{qid}", body)
        updated += 1
    print(f"updated {updated} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
