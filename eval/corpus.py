"""The frozen corpus: a deterministic, stratified sample of REAL dev articles.

Why stratified and not random: a uniform sample of ``raw_articles`` is 99.2%
English and 21% USGS/GDACS sensor alerts, so a random 30 would contain almost no
non-English copy and almost no wire copy -- exactly the populations the
enrichment chain is weakest on. The strata below are fixed and the pick inside
each stratum is ``ORDER BY`` a stable key, so re-running this script on the same
dev data reproduces the same 30 rows. The pick is frozen into
``eval/data/corpus.json``; the script exists to prove how the freeze was made,
not to be re-run casually (dev is a moving target).

Deliberate skews, stated so a reader can discount them:
  * non-English is oversampled roughly 10x versus the corpus (10 of 30 against
    2.0% of rows). This is an instrument for tuning, and the cases worth tuning
    on are the ones the corpus under-represents. The per-language weights are
    recorded in every row.
  * sensor alerts (USGS/GDACS) are undersampled versus the corpus (21% of rows,
    2 of 30 here): they are short templated bulletins with a fixed shape, and
    one stratum is enough to know the extractor handles them.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

DATA = Path(__file__).resolve().parent / "data"
CORPUS_PATH = DATA / "corpus.json"

# Stratum name -> SQL fragment restricting that stratum, and the target count.
# Order matters: the first stratum to match a row wins, so narrow strata come
# first and the catch-alls come last.
STRATA: list[tuple[str, str, int]] = [
    # --- non-English: one per language, longest body in that language -------- #
    ("lang-fa", "detected_language = 'fa'", 1),
    ("lang-zh", "detected_language = 'zh-cn'", 1),
    ("lang-ml", "detected_language = 'ml'", 1),
    ("lang-sq", "detected_language = 'sq'", 1),
    ("lang-uk", "detected_language = 'uk'", 1),
    ("lang-tr", "detected_language = 'tr'", 1),
    ("lang-ro", "detected_language = 'ro'", 1),
    ("lang-fi", "detected_language = 'fi'", 1),
    ("lang-de", "detected_language = 'de'", 1),
    ("lang-es", "detected_language = 'es'", 1),
    ("lang-id", "detected_language = 'id'", 1),
    ("lang-it", "detected_language = 'it'", 1),
    ("lang-fr", "detected_language = 'fr'", 1),
    # --- English, by body length (the prompt truncates at 8000 chars) -------- #
    ("en-short", "detected_language = 'en' AND length(body_text) < 400", 3),
    ("en-medium", "detected_language = 'en' AND length(body_text) BETWEEN 1200 AND 3000", 3),
    ("en-long", "detected_language = 'en' AND length(body_text) > 8000", 3),
    # --- English, by source archetype -------------------------------------- #
    ("wire-cable", "detected_language = 'en' AND source_domain IN "
                   "('france24.com','trend.az','middleeasteye.net','allafrica.com')", 2),
    ("institutional", "detected_language = 'en' AND source_domain IN "
                      "('un.org','who.int','csis.org','foreignaffairs.com','foreignpolicy.com')", 2),
    ("ugc-reddit", "detected_language = 'en' AND source_domain = 'reddit.com'", 2),
    ("sensor-alert", "detected_language = 'en' AND source_domain IN "
                     "('earthquake.usgs.gov','gdacs.org')", 2),
]


@dataclass
class CorpusItem:
    article_id: str
    url: str
    title: str
    source_domain: str
    source_tier: str | None
    detected_language: str | None
    body_chars: int
    body_sha256: str
    stratum: str
    body_text: str

    def as_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        d = {
            "article_id": self.article_id,
            "url": self.url,
            "title": self.title,
            "source_domain": self.source_domain,
            "source_tier": self.source_tier,
            "detected_language": self.detected_language,
            "body_chars": self.body_chars,
            "body_sha256": self.body_sha256,
            "stratum": self.stratum,
        }
        if include_body:
            d["body_text"] = self.body_text
        return d


def dev_dsn() -> str:
    """Full userinfo for the local tunnel. Never printed, never logged."""
    pw = os.environ["SUPABASE_DB_PASSWORD"]
    return urlunsplit(("postgresql", f"postgres:{pw}@127.0.0.1:15432", "/postgres", "", ""))


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def select(conn, *, verbose: bool = True) -> list[CorpusItem]:
    """Run the selection rule against dev. Read-only SELECTs only."""
    import psycopg

    chosen: list[CorpusItem] = []
    seen: set[str] = set()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_articles WHERE body_text IS NOT NULL AND length(body_text) > 200")
        corpus_size = cur.fetchone()[0]
        for name, where, target in STRATA:
            # Longest body first inside a stratum: the longest copy in a language
            # is the hardest one, and a stable tiebreak keeps it reproducible.
            cur.execute(
                f"""
                SELECT id::text, url, coalesce(title,''), source_domain, source_tier::text,
                       detected_language, body_text
                FROM raw_articles
                WHERE body_text IS NOT NULL AND length(body_text) > 200 AND ({where})
                  AND id::text <> ALL(%s)
                ORDER BY length(body_text) DESC, id::text ASC
                LIMIT %s
                """,
                (list(seen), target),
            )
            got = 0
            for row in cur.fetchall():
                aid, url, title, domain, tier, lang, body = row
                if aid in seen:
                    continue
                seen.add(aid)
                got += 1
                chosen.append(
                    CorpusItem(
                        article_id=aid, url=url, title=title, source_domain=domain,
                        source_tier=tier, detected_language=lang, body_chars=len(body),
                        body_sha256=body_hash(body), stratum=name, body_text=body,
                    )
                )
            if verbose:
                print(f"  stratum {name:<14} target={target} got={got}")
    if verbose:
        print(f"  corpus rows eligible: {corpus_size}; frozen: {len(chosen)}")
    return chosen


def freeze(items: list[CorpusItem], *, path: Path | str = CORPUS_PATH) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "corpus_version": "evalset-corpus/v1",
        "source": "supabase dev project news-pipeline-dev, table raw_articles",
        "selection_rule": (
            "20 fixed strata (see eval/corpus.py STRATA), first-match-wins, "
            "longest-body-first inside each stratum, deterministic on (length DESC, id ASC). "
            "One row per non-English language; English split by body length, source "
            "archetype (wire/institutional/UGC/sensor). Non-English is oversampled ~10x "
            "and sensor alerts undersampled ~10x versus the corpus, deliberately: the "
            "instrument is for tuning the populations the chain is weakest on."
        ),
        "eligible_rows": 2630,
        "count": len(items),
        # Bodies are deliberately NOT committed: the freeze pins id/url/sha256/stratum and
        # the text is re-read from dev at run time and hash-checked. See load().
        "items": [i.as_dict(include_body=False) for i in items],
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load(path: Path | str = CORPUS_PATH) -> list[CorpusItem]:
    """Load the frozen corpus, with bodies, from dev.

    Bodies are NOT committed: they are the real article text and the corpus is
    meant to stay pinned to the rows that existed when it was frozen. The
    committed file carries the id, url, sha256 and stratum; the text is re-read
    from dev at run time and its sha256 is checked against the freeze. If a body
    ever changes, the run fails loudly rather than silently scoring new text
    against old labels.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[CorpusItem] = []
    missing: list[str] = []
    with _psycopg_connect() as conn:
        with conn.cursor() as cur:
            for item in data["items"]:
                cur.execute("SELECT title, body_text FROM raw_articles WHERE id = %s", (item["article_id"],))
                row = cur.fetchone()
                if row is None:
                    missing.append(item["article_id"])
                    continue
                title, body = row
                got = body_hash(body or "")
                if got != item["body_sha256"]:
                    raise RuntimeError(
                        f"body changed for frozen article {item['article_id']} "
                        f"({item['url']}): expected {item['body_sha256'][:12]}, got {got[:12]}. "
                        "The corpus is frozen; re-freeze deliberately or drop the row."
                    )
                out.append(
                    CorpusItem(
                        article_id=item["article_id"], url=item["url"], title=title or "",
                        source_domain=item["source_domain"], source_tier=item["source_tier"],
                        detected_language=item["detected_language"],
                        body_chars=len(body or ""), body_sha256=got,
                        stratum=item["stratum"], body_text=body or "",
                    )
                )
    if missing:
        raise RuntimeError(f"{len(missing)} frozen articles are gone from dev: {missing}")
    if len(out) != data["count"]:
        raise RuntimeError(f"expected {data['count']} corpus rows, loaded {len(out)}")
    return out


def _psycopg_connect():
    import psycopg

    return psycopg.connect(dev_dsn())


def main() -> int:
    print("selecting frozen corpus from dev...")
    with _psycopg_connect() as conn:
        items = select(conn)
    p = freeze(items)
    print(f"wrote {p} ({p.stat().st_size} bytes, {len(items)} articles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
