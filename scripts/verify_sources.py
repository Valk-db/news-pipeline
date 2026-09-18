"""Verify that tier-1 sources (AP, Reuters) actually return articles via GDELT."""

import asyncio
from src.ingestion.gdelt import verify_sources


async def main():
    print("Verifying tier-1 sources via GDELT...")
    print("=" * 50)

    results = await verify_sources()

    print("\nResults:")
    for domain, count in results.items():
        status = "✓" if count > 0 else "✗"
        print(f"  {status} {domain}: {count} articles in last 24h")

    if all(c > 0 for c in results.values()):
        print("\n✓ All tier-1 sources verified. Safe to run pipeline.")
    else:
        print("\n⚠ Some tier-1 sources returned zero articles.")
        print("  Check GDELT API availability or domain filters.")


if __name__ == "__main__":
    asyncio.run(main())