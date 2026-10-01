#!/usr/bin/env python
"""Backfill the scheme u1 URL columns on raw_articles.

Reads every raw_articles row that has no url_hash_v1 yet, canonicalizes its
url with canonicalize_url_v1, hashes that with compute_url_hash, and writes

  raw_articles.canonical_url_v1
  raw_articles.url_hash_v1
  url_aliases, one row per legacy url_hash, mapping it to the u1 identity

It never writes raw_articles.url or raw_articles.url_hash. Those two columns are
what the signed Merkle log already refers to, so they are frozen. A u1 hash
that two legacy rows collapse onto is reported, never resolved here, because
deciding which row survives is the ingest workstream's call and needs the
unique constraint to come later.

Idempotent by construction. A row with a url_hash_v1 already set is skipped, so
a re-run after a partial failure picks up exactly the rows still missing. An
existing url_aliases row for the same legacy hash is updated rather than
duplicated, so a re-run converges instead of failing.

Credentials come from the repo's usual place, DATABASE_URL via get_settings,
which reads the .env file and the environment. Nothing is hardcoded here.

Usage:
    uv run python scripts/backfill_url_hash_v1.py --dry-run
    uv run python scripts/backfill_url_hash_v1.py --limit 500
    uv run python scripts/backfill_url_hash_v1.py
"""

import argparse
import asyncio
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import RawArticle, UrlAlias
from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker
from src.utils.trafilatura_extract import canonicalize_url_v1, compute_url_hash

BATCH_SIZE = 200


def derive(url: Optional[str]) -> Tuple[str, str]:
    """Return (canonical_url_v1, url_hash_v1) for a stored url."""
    canonical = canonicalize_url_v1(url or "")
    return canonical, compute_url_hash(url or "")


async def count_pending(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(RawArticle).where(
        RawArticle.url_hash_v1.is_(None)
    )
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def backfill(
    dry_run: bool = True,
    limit: int = 0,
    batch_size: int = BATCH_SIZE,
) -> Dict[str, object]:
    settings = get_settings()
    if not settings.has_database:
        raise SystemExit(
            "ERROR: DATABASE_URL is not configured. Set it in the environment or "
            "in the repo .env file, the same way the rest of the scripts read it."
        )
    if _get_engine() is None or _get_session_maker() is None:
        raise SystemExit("ERROR: could not build an async engine from DATABASE_URL")

    session_maker = _get_session_maker()
    totals = {
        "pending": 0,
        "scanned": 0,
        "updated": 0,
        "aliases": 0,
        "collisions": 0,
        "collision_examples": [],
    }
    # v1 hash to the legacy hashes that produced it, for the collision report.
    owners: Dict[str, List[str]] = defaultdict(list)

    async with session_maker() as session:
        totals["pending"] = await count_pending(session)

        stmt = (
            select(RawArticle.id, RawArticle.url, RawArticle.url_hash)
            .where(RawArticle.url_hash_v1.is_(None))
            .order_by(RawArticle.fetched_at)
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        rows = result.all()

        for start in range(0, len(rows), batch_size):
            chunk = rows[start:start + batch_size]
            updates = []
            aliases = []
            for article_id, url, old_hash in chunk:
                canonical, hash_v1 = derive(url)
                totals["scanned"] += 1
                updates.append({
                    "id": article_id,
                    "canonical_url_v1": canonical,
                    "url_hash_v1": hash_v1,
                })
                aliases.append({
                    "old_url_hash": old_hash,
                    "url_hash_v1": hash_v1,
                    "canonical_url_v1": canonical,
                })
                owners[hash_v1].append(old_hash)

            if dry_run:
                continue

            for update in updates:
                await session.execute(
                    RawArticle.__table__.update()
                    .where(RawArticle.__table__.c.id == update["id"])
                    .values(
                        canonical_url_v1=update["canonical_url_v1"],
                        url_hash_v1=update["url_hash_v1"],
                    )
                )
            totals["updated"] += len(updates)

            # One query for the whole batch instead of an upsert per row, so the
            # script stays dialect agnostic. url_aliases.old_url_hash is the
            # primary key, so at most one existing row can match each alias.
            old_hashes = [alias["old_url_hash"] for alias in aliases]
            existing = await session.execute(
                select(UrlAlias).where(UrlAlias.old_url_hash.in_(old_hashes))
            )
            known = {row.old_url_hash: row for row in existing.scalars().all()}
            inserted = 0
            for alias in aliases:
                row = known.get(alias["old_url_hash"])
                if row is None:
                    session.add(UrlAlias(**alias))
                    inserted += 1
                else:
                    row.url_hash_v1 = alias["url_hash_v1"]
                    row.canonical_url_v1 = alias["canonical_url_v1"]
            totals["aliases"] += inserted
            await session.commit()

        if not dry_run:
            await session.commit()

    collisions = []
    for hash_v1, old_hashes in owners.items():
        distinct = sorted(set(old_hashes))
        if len(distinct) > 1:
            totals["collisions"] += 1
            collisions.append((hash_v1, distinct))
    # Sorted by hash so two runs over the same data print the same examples.
    collisions.sort()
    totals["collision_examples"] = collisions[:5]

    return totals


def report(totals: Dict[str, object], dry_run: bool, limit: int) -> None:
    prefix = "dry run, would write" if dry_run else "wrote"
    print(f"raw_articles rows missing url_hash_v1: {totals['pending']}")
    print(f"scanned {totals['scanned']} rows")
    print(f"{prefix} canonical_url_v1 and url_hash_v1 on {totals['updated']} rows")
    print(f"{prefix} {totals['aliases']} url_aliases rows")
    if limit:
        print(f"stopped at the limit of {limit}")
    print("raw_articles.url and raw_articles.url_hash were not read for writing "
          "and were not modified")
    collisions = int(totals["collisions"])
    if not collisions:
        print("no u1 hash is claimed by more than one legacy url_hash")
        return
    print(
        f"WARNING: {collisions} scheme u1 hash values are claimed by more than one "
        "legacy url_hash. Those rows are one article under u1 that the legacy "
        "scheme kept apart. Nothing was merged here, and url_hash_v1 stays "
        "non unique until the ingest workstream decides what to do with them."
    )
    for hash_v1, old_hashes in totals["collision_examples"]:
        print(f"  u1 {hash_v1[:12]} from {len(old_hashes)} legacy hashes")
        for old_hash in old_hashes:
            print(f"    {old_hash}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate the scheme u1 URL columns without touching url or url_hash"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and report, write nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many rows, 0 means no limit")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"rows per commit, default {BATCH_SIZE}")
    args = parser.parse_args()

    totals = asyncio.run(
        backfill(dry_run=args.dry_run, limit=args.limit, batch_size=args.batch_size)
    )
    report(totals, args.dry_run, args.limit)


if __name__ == "__main__":
    main()
