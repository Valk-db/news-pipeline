"""Seed ownership graph (reference - also embedded in units.py)."""

import asyncio
from src.verification.units import OWNERSHIP_GROUPS


async def main():
    print("Ownership Groups (20 core relationships)")
    print("=" * 50)

    groups = {}
    for domain, group in OWNERSHIP_GROUPS.items():
        groups.setdefault(group, []).append(domain)

    for group, domains in sorted(groups.items()):
        print(f"\n{group}:")
        for domain in sorted(domains):
            print(f"  {domain}")

    print(f"\nTotal: {len(OWNERSHIP_GROUPS)} domains mapped to {len(groups)} groups")


if __name__ == "__main__":
    asyncio.run(main())