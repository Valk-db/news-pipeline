"""The single command that prints the scoreboard.

    python -m eval.run                  # score the frozen corpus (live or replay)
    python -m eval.run --limit 5        # first N corpus rows, for a smoke test
    python -m eval.run --no-cache       # force live calls, ignore the replay cache
    python -m eval.run --model groq/... # score against a different model id
    python -m eval.run --json out.json  # machine-readable scoreboard

Cost accounting and the Groq free-tier headroom report are part of the output,
because "free" is a budget, not an absence of cost.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from eval.adapter import TransportShim, UsageRecorder
from eval.cache import ReplayCache
from eval.corpus import CorpusItem, load
from eval.gold import GoldLabel, load as load_gold
from eval.scoring import (
    FIELDS,
    FieldScore,
    GoldSnippet,
    PredSnippet,
    aggregate,
    format_scoreboard,
    macro_f1,
    score_article,
    script_diagnostic,
)

# Groq's documented free tier. Hard numbers, quoted so a reader can check them
# rather than taking the harness's word for what "free" means today.
GROQ_FREE_RPM = 20
GROQ_FREE_RPD = 1000
# Minimum wall-clock gap between LIVE provider calls, from GROQ_FREE_RPM. The
# Groq free tier returns 429 above 20 requests/minute, and the production retry
# (tenacity, 3 attempts, 1-10s) would burn its attempts and then record a
# provider error for an article that never got asked. Pacing keeps the baseline
# a measurement of extraction rather than of rate limiting. Replayed calls are not
# paced: nothing leaves the machine.
GROQ_MIN_INTERVAL_SECONDS = 60.0 / GROQ_FREE_RPM + 0.5
# The repo's own caps, from src/shared/config.py:46 and
# src/enrichment/translation.py:86. The eval deliberately does NOT spend these:
# it uses a throwaway SQLite counter so a full eval run cannot eat the daily
# budget the pipeline depends on.
REPO_GROQ_DAILY_BUDGET = 900


def _fix_no_proxy_for_httpx() -> list[str]:
    """This VM's NO_PROXY contains a literal ``[::1]``, which httpx cannot parse.

    ``httpx.Client`` raises ``InvalidURL: Invalid port: ':1]'`` at construction, so
    the Groq SDK (which is httpx under the hood) cannot even be built. It looks like
    a network failure and is really a config parse failure. Removed here rather than
    at the call site so that the harness cannot be run the wrong way and produce
    numbers that are not about extraction at all.

    Nothing here needs the bypass: the Groq client talks to api.groq.com through the
    egress proxy, and the dev Postgres connection in eval/corpus.py is a raw TCP
    socket to 127.0.0.1 that does not read NO_PROXY. Only ``curl`` to localhost
    genuinely needs the variable, and the harness does not use it.
    """
    removed = []
    for name in ("NO_PROXY", "no_proxy"):
        if name in os.environ:
            removed.append(f"{name}={os.environ.pop(name)}")
    return removed


def _install_scratch_budget() -> None:
    """Give the production budget counter a throwaway SQLite backend.

    ``RequestBudget`` is the pipeline's real gate and the eval runs through the
    real client, so the counter has to work. Pointing it at a scratch file keeps
    the production code path (the cap is real, ``spend`` really runs) while
    leaving dev's ``groq_requests`` row alone -- otherwise every eval run would
    spend budget the scheduled ingest needs.
    """
    import asyncio as _asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from src.shared import budget as budget_module

    path = Path(tempfile.gettempdir()) / "evalset-budget-counter.sqlite"
    if path.exists():
        path.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE budget_counters ("
                "name TEXT NOT NULL, day DATE NOT NULL, used BIGINT NOT NULL DEFAULT 0,"
                "PRIMARY KEY (name, day))"
            )

    _asyncio.run(_create())
    budget_module._get_engine = lambda: engine  # type: ignore[attr-defined]


def _parse_predicted(snippets: list[dict[str, Any]]) -> list[PredSnippet]:
    """Map the production post-processing output onto PredSnippet.

    ``extract_snippets_from_article`` has already clamped confidence into 0-100
    and scaled position by 1_000_000 (snippet_extractor.py:108-109), so undo
    the scale here rather than teaching the scorer about the storage format.
    """
    out: list[PredSnippet] = []
    for s in snippets:
        pos = s.get("position")
        if isinstance(pos, (int, float)) and float(pos) > 1.0:
            pos = float(pos) / 1_000_000.0
        out.append(
            PredSnippet(
                text=s.get("text", ""),
                type=s.get("snippet_type") or s.get("type"),
                entities=list(s.get("entities") or []),
                confidence=s.get("confidence"),
                position=pos,
            )
        )
    return out


class _NullStats:
    """Accepts and discards the in-memory stats calls the extractor makes.

    The real IngestStats.record raises TypeError on the one call site
    (snippet_extractor.py:112 passes count=, the signature takes n=), and that
    raise happens *after* a successful extraction inside the same try block, so
    the except at line 115 returns []. Neutralising the recorder lets the
    harness observe the extraction the pipeline throws away.
    """

    def record(self, *args: Any, **kwargs: Any) -> None:
        return None


async def run_corpus(
    items: list[CorpusItem],
    gold: dict[str, GoldLabel],
    *,
    cache: ReplayCache,
    model_override: str | None = None,
    max_snippets: int = 5,
    mutate: str | None = None,
) -> dict[str, Any]:
    """Run the real extraction over the corpus, scoring fields as we go."""
    from unittest.mock import patch

    from src.enrichment import snippet_extractor
    from src.shared.llm import LLMClient

    usage = UsageRecorder()
    client = LLMClient()
    usage.install(client)

    rows: list[tuple[str, str, dict[str, FieldScore]]] = []
    diagnostics: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    responses: list[dict[str, Any]] = []
    mutation_notes: list[str] = []
    last_live = 0.0

    def pace(after: float) -> float:
        """Wait out the remainder of the free-tier window between live calls."""
        nonlocal last_live
        if cache.stats.requests <= after:
            return after
        now = time.monotonic()
        wait = last_live + GROQ_MIN_INTERVAL_SECONDS - now
        if wait > 0:
            print(f"    pacing {wait:.1f}s to stay under {GROQ_FREE_RPM} req/min")
            time.sleep(wait)
        last_live = time.monotonic()
        return cache.stats.requests

    def on_response(key, model, p_hash, i_hash, content, replayed, error=None):
        responses.append(
            {
                "key": key, "model": model, "prompt_hash": p_hash, "input_hash": i_hash,
                "replayed": replayed, "chars": len(content or ""),
                "signature": __import__("hashlib").sha256((content or "").encode()).hexdigest()[:16],
                "error": error,
            }
        )

    try:
        for n, item in enumerate(items, 1):
            label = gold.get(item.article_id)
            print(f"  [{n:>2}/{len(items)}] {item.article_id[:8]} {item.stratum:<14} "
                  f"{str(item.detected_language):<6} body={len(item.body_text):>6} "
                  f"live={cache.stats.requests} hits={cache.stats.hits}", flush=True)
            shim = TransportShim(
                client, cache,
                article_id=item.article_id, body=item.body_text,
                knobs={"max_snippets": max_snippets},
                model_override=model_override, usage=usage, on_response=on_response,
                mutate=mutate,
            )
            # The production function, called verbatim. Two names are redirected,
            # and neither is the prompt or any extraction logic:
            #   get_llm_client -- production calls it WITHOUT await (line 42) and then
            #     uses llm.chat / llm.model, which LLMClient does not have (line 63).
            #     The shim supplies exactly those two attributes.
            #   STATS -- snippet_extractor.py:112 calls record(..., count=N) but the
            #     signature is record(source, event, n=1) (src/utils/ingest_stats.py:20).
            #     That TypeError is raised after a SUCCESSFUL extraction, inside the
            #     same try block, so the except at line 115 turns a good result into
            #     an empty list. It is a stats side-channel, not extraction, so it is
            #     neutralised here; both defects are reported, neither is fixed.
            with patch.object(snippet_extractor, "get_llm_client", lambda: shim), \
                    patch.object(snippet_extractor, "STATS", _NullStats()):
                produced = await snippet_extractor.extract_snippets_from_article(
                    article_id=item.article_id,
                    story_id="00000000-0000-0000-0000-000000000000",
                    text=item.body_text,
                    title=item.title,
                    max_snippets=max_snippets,
                )
            last_live = pace(last_live)
            mutation_notes.extend(shim.mutation_notes)

            pred = _parse_predicted(list(produced or []))
            diagnostics.append(
                {
                    "article_id": item.article_id[:8],
                    "stratum": item.stratum,
                    "lang": item.detected_language,
                    "gold_snippets": len(label.snippets) if label else 0,
                    "pred_snippets": len(pred),
                    "script_overlap": round(script_diagnostic(item.body_text, pred), 3),
                }
            )
            if label is None:
                failures.append({"article_id": item.article_id, "why": "no gold label"})
                continue
            gold_snips = [GoldSnippet(**s) for s in label.snippets]
            scores = score_article(gold_snips, pred)
            rows.append((item.article_id, item.stratum, scores))
    finally:
        usage.uninstall()
        await client.close()

    overall = aggregate(s for _, _, s in rows)
    return {
        "rows": rows,
        "overall": overall,
        "diagnostics": diagnostics,
        "unlabeled": failures,
        "responses": responses,
        "stats": cache.stats.as_dict(),
        "mutation": mutate,
        "mutation_notes": mutation_notes,
    }


def headroom(requests: int) -> dict[str, Any]:
    return {
        "groq_free_rpm": GROQ_FREE_RPM,
        "groq_free_rpd": GROQ_FREE_RPD,
        "requests_this_run": requests,
        "daily_headroom_after_run": GROQ_FREE_RPD - requests,
        "daily_used_pct": round(100.0 * requests / GROQ_FREE_RPD, 2),
        "within_daily_cap": requests <= GROQ_FREE_RPD,
        "repo_pipeline_daily_budget": REPO_GROQ_DAILY_BUDGET,
        "eval_spends_pipeline_budget": False,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.run")
    ap.add_argument("--limit", type=int, default=0, help="score only the first N corpus rows")
    ap.add_argument("--no-cache", action="store_true", help="ignore the replay cache and call the provider")
    ap.add_argument("--model", default=None, help="override the model id (baseline uses the configured one)")
    ap.add_argument("--max-snippets", type=int, default=5)
    ap.add_argument("--json", default=None, help="also write the scoreboard as JSON")
    ap.add_argument(
        "--mutate", default=None,
        help="apply a named corruption from eval/mutate.py after the cache lookup; "
             "proves the harness can see a regression. Costs no provider requests.",
    )
    args = ap.parse_args(argv)

    if not os.environ.get("GROQ_API_KEY"):
        print("GROQ_API_KEY is not set. The replay cache can still serve a run, "
              "but a cold run cannot. Aborting.", file=sys.stderr)
        return 2

    removed = _fix_no_proxy_for_httpx()
    if removed:
        print("note: unset for this run (httpx cannot parse this VM's NO_PROXY): "
              + ", ".join(r.split("=")[0] for r in removed))
    _install_scratch_budget()
    items = load()
    gold = load_gold()
    # --limit means "score the first N HAND-LABELED articles", so a smoke test
    # with a small N lands on scored work rather than on an unlabeled row.
    labeled = [i for i in items if i.article_id in gold]
    unlabeled = [i for i in items if i.article_id not in gold]
    if args.limit:
        labeled = labeled[: args.limit]
    print(f"corpus: {len(items)} frozen articles, {len(labeled)} hand-labeled and scored, "
          f"{len(unlabeled)} unlabeled (not scored, listed below)")
    print(f"cache:  {'OFF (forcing live calls)' if args.no_cache else 'warm (record-and-replay)'}")
    print()

    if args.mutate:
        print(f"MUTATION ARMED: {args.mutate} (see python -m eval.mutate). "
              "Applied after the cache lookup, never written back.")

    cache = ReplayCache(enabled=not args.no_cache)
    result = asyncio.run(
        run_corpus(labeled, gold, cache=cache, model_override=args.model,
                   max_snippets=args.max_snippets, mutate=args.mutate)
    )

    print(format_scoreboard(result["rows"], result["overall"]))
    print()
    print(f"MACRO mean-F1 across {len(FIELDS)} fields: {macro_f1(result['overall']):.4f}")
    print()
    print("per-article diagnostics (pred_snippets=0 means the stage returned nothing):")
    print(f"  {'article':<10} {'stratum':<14} {'lang':<6} {'gold':>4} {'pred':>4} {'script_ovl':>10}")
    for d in result["diagnostics"]:
        print(f"  {d['article_id']:<10} {d['stratum']:<14} {str(d['lang']):<6} "
              f"{d['gold_snippets']:>4} {d['pred_snippets']:>4} {d['script_overlap']:>10.3f}")
    if result["unlabeled"]:
        print()
        print("UNLABELED (not scored):")
        for f in result["unlabeled"]:
            print(f"  {f['article_id'][:8]}  {f['why']}")
    print()
    stats = result["stats"]
    print("cost per run:")
    print(f"  live provider requests : {stats['requests']}")
    print(f"  replay cache hits      : {stats['hits']}")
    print(f"  cache misses           : {stats['misses']}")
    print(f"  cache writes           : {stats['writes']}")
    print(f"  prompt tokens          : {stats['prompt_tokens']}")
    print(f"  completion tokens      : {stats['completion_tokens']}")
    print(f"  total tokens           : {stats['total_tokens']}")
    print(f"  provider errors        : {stats['errors']}")
    print(f"  models                 : {stats['models']}")
    print("  free-tier headroom     : " + json.dumps(headroom(stats["requests"])))

    if args.json:
        payload = {
            "overall": {k: v.as_dict() for k, v in result["overall"].items()},
            "macro_f1": round(macro_f1(result["overall"]), 4),
            "per_article": [
                {"article_id": a, "stratum": s, **{k: v.as_dict() for k, v in sc.items()}}
                for a, s, sc in result["rows"]
            ],
            "diagnostics": result["diagnostics"],
            "unlabeled": result["unlabeled"],
            "mutation": result["mutation"],
            "mutation_notes": result["mutation_notes"],
            "cost": stats,
            "headroom": headroom(stats["requests"]),
            "responses": result["responses"],
        }
        Path(args.json).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
