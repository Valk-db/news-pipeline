"""Seed source tier classifications (already in code, but useful for reference)."""

import asyncio
from src.verification.tiers import TIER1_DOMAINS, TIER2_DOMAINS, classify_source_tier


async def main():
    print("Source Tier Definitions")
    print("=" * 50)

    print("\nTIER 1 (Verified editorial standards):")
    for domain in sorted(TIER1_DOMAINS):
        print(f"  {domain}")

    print("\nTIER 2 (Reputable, non-wire):")
    for domain in sorted(TIER2_DOMAINS):
        print(f"  {domain}")

    print("\nTest classifications:")
    test_domains = ["bbc.com", "nytimes.com", "randomblog.com", "apnews.com", "reddit.com"]
    for domain in test_domains:
        tier = classify_source_tier(domain)
        print(f"  {domain} -> {tier.value}")


if __name__ == "__main__":
    asyncio.run(main())