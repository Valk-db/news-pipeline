#!/usr/bin/env python
"""Check that the free model ids in llm_roster.ROSTER still exist upstream.

A `:free` model id is a perishable fact. OpenRouter retires free tiers, and
``openai/gpt-oss-20b:free`` -- the backup this roster was originally asked to wire up --
is gone from the entire 466-model catalogue, not merely from the free subset.

The reason this is a script rather than a log line is that the failure it guards against
is invisible without it. With a fallback chain in front of it, a retired id produces a
404 on every request to that rung, which reads exactly like "the free pool is busy
today": the chain falls through, nobody is told why, and the rung is demoted for the run
for a reason that will still be true tomorrow. The staleness check turns that silent
wrong answer into a loud, early one.

Exit codes are three-valued on purpose. "Could not check" is not "everything is fine",
and a check that cannot reach OpenRouter must not report a pass -- but it also must not
be indistinguishable from a confirmed-retired id, because the two call for different
responses::

    0  every configured free id is in the catalogue
    1  at least one configured free id is stale (retired or renamed)
    2  the catalogue could not be read, or no OpenRouter key is configured

Usage:
    uv run python scripts/check_free_models.py
    OPENROUTER_API_KEY=... uv run python scripts/check_free_models.py
"""

import asyncio
import sys
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not the repo root, so `src` is
# unimportable without this. Cheap, and it keeps the documented `python scripts/...`
# invocation working from a plain checkout with no PYTHONPATH set.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.shared.config import get_settings
from src.shared.llm_roster import check_free_model_roster


async def _main() -> int:
    settings = get_settings()
    if not settings.has_openrouter:
        print(
            "OPENROUTER_API_KEY is not set: there are no free ids to check.\n"
            "The roster's free rungs stay in the file and are skipped at runtime."
        )
        return 2

    check = await check_free_model_roster(settings)
    print(check.render())
    if not check.ok:
        return 1 if not check.error else 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
