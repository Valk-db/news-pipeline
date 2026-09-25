#!/usr/bin/env python
"""Weekly enrichment run: media, video, social, and embedding enrichment
for recently-ingested stories.

Used by .github/workflows/weekly-enrichment.yml.

Usage:
    uv run python scripts/run_weekly_enrichment.py
"""

import asyncio
import json
import os

from src.shared.database import init_db, get_session
from src.enrichment.pipeline import enrich_recent_stories


async def main() -> None:
    await init_db()
    async with get_session() as session:
        results = await enrich_recent_stories(session, hours_back=168, max_stories=100)
        print(f"Enriched {len(results)} stories")

        total_media = 0
        total_videos = 0
        total_social = 0
        total_embeddings = 0
        for r in results:
            if r.get("errors"):
                print(f"  Story {r['story_id']}: {len(r['errors'])} errors")
            else:
                print(
                    f"  Story {r['story_id']}: media={r['media_assets']} "
                    f"videos={r['videos_found']} snippets={r['social_snippets_found']} "
                    f"embeddings={r['embeddings_generated']}"
                )
                total_media += r.get("media_assets", 0)
                total_videos += r.get("videos_found", 0)
                total_social += r.get("social_snippets_found", 0)
                total_embeddings += r.get("embeddings_generated", 0)

        summary = {
            "stories_processed": len(results),
            "total_media_assets": total_media,
            "total_videos_found": total_videos,
            "total_social_snippets": total_social,
            "total_embeddings_generated": total_embeddings,
        }
        summary_line = f"ENRICHMENT_SUMMARY={json.dumps(summary)}"
        print(summary_line)

        # Also write it as a step output for the workflow's follow-up check.
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(summary_line + "\n")


if __name__ == "__main__":
    asyncio.run(main())