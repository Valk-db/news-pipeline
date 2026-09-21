#!/usr/bin/env python3
"""
P95 latency budget check for CI gate.

Runs a quick ingestion benchmark and checks P95 latency against threshold.

Usage:
    uv run scripts/check_latency.py [--threshold-ms=5000]
"""

import argparse
import asyncio
import time
import statistics
from datetime import datetime, timezone

from src.shared.database import init_db, get_session
from src.ingestion.rss import ingest_rss_feeds
from src.ingestion.gdelt import ingest_gdelt
from src.ingestion.reddit import ingest_reddit
from src.verification.units import build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate


async def benchmark_phase(name: str, func, *args, **kwargs) -> float:
    """Run a phase and return duration in milliseconds."""
    start = time.perf_counter()
    await func(*args, **kwargs)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms


async def run_benchmark(iterations: int = 3) -> dict:
    """Run ingestion pipeline benchmark."""
    await init_db()

    results = {
        "ingest_rss": [],
        "ingest_gdelt": [],
        "ingest_reddit": [],
        "build_units": [],
        "build_stories": [],
        "apply_gate": [],
        "total": [],
    }

    for i in range(iterations):
        print(f"Benchmark iteration {i + 1}/{iterations}...")

        async with get_session() as session:
            # Phase 1: Ingestion (with minimal data)
            rss_start = time.perf_counter()
            rss_articles = await ingest_rss_feeds(max_per_feed=5)
            results["ingest_rss"].append((time.perf_counter() - rss_start) * 1000)

            gdelt_start = time.perf_counter()
            gdelt_articles, _ = await ingest_gdelt(hours_back=1, max_per_domain=10)
            results["ingest_gdelt"].append((time.perf_counter() - gdelt_start) * 1000)

            reddit_start = time.perf_counter()
            reddit_articles = await ingest_reddit(limit_per_sub=5)
            results["ingest_reddit"].append((time.perf_counter() - reddit_start) * 1000)

            # Phase 2: Build reporting units
            units_start = time.perf_counter()
            await build_reporting_units(session)
            results["build_units"].append((time.perf_counter() - units_start) * 1000)

            # Phase 3: Build stories
            stories_start = time.perf_counter()
            await build_stories(session)
            results["build_stories"].append((time.perf_counter() - stories_start) * 1000)

            # Phase 4: Apply gate
            gate_start = time.perf_counter()
            await apply_tier1_gate(session)
            results["apply_gate"].append((time.perf_counter() - gate_start) * 1000)

            # Total
            total = (results["ingest_rss"][-1] + results["ingest_gdelt"][-1] + results["ingest_reddit"][-1] +
                     results["build_units"][-1] + results["build_stories"][-1] + results["apply_gate"][-1])
            results["total"].append(total)

    # Compute P95
    def p95(values):
        if not values:
            return 0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    summary = {}
    for phase, times in results.items():
        if times:
            summary[phase] = {
                "mean_ms": round(statistics.mean(times), 1),
                "median_ms": round(statistics.median(times), 1),
                "p95_ms": round(p95(times), 1),
                "max_ms": round(max(times), 1),
            }

    return summary


async def main():
    parser = argparse.ArgumentParser(description="Check P95 latency budget")
    parser.add_argument("--threshold-ms", type=int, default=5000, help="P95 threshold in ms")
    parser.add_argument("--iterations", type=int, default=3, help="Benchmark iterations")
    args = parser.parse_args()

    print(f"Running latency benchmark ({args.iterations} iterations)...")
    summary = await run_benchmark(args.iterations)

    print("\nLatency Summary (ms):")
    for phase, stats in summary.items():
        print(f"  {phase}: mean={stats['mean_ms']}, median={stats['median_ms']}, p95={stats['p95_ms']}, max={stats['max_ms']}")

    # Check total P95 against threshold
    total_p95 = summary.get("total", {}).get("p95_ms", 0)
    print(f"\nTotal P95: {total_p95}ms (threshold: {args.threshold_ms}ms)")

    if total_p95 > args.threshold_ms:
        print(f"❌ FAIL: P95 latency ({total_p95}ms) exceeds threshold ({args.threshold_ms}ms)")
        return 1
    else:
        print(f"✅ PASS: P95 latency ({total_p95}ms) within threshold ({args.threshold_ms}ms)")
        return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))