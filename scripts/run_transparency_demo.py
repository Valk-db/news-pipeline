#!/usr/bin/env python
"""End-to-end demo of the signed Merkle transparency log.

Builds a log of synthetic observations (no network, no database), signs and
anchors a checkpoint every K entries, then prints the inclusion proof for one
entry and verifies it end to end against its checkpoint, plus two negative
controls so a green run means something.

Usage:
    uv run python scripts/run_transparency_demo.py
    uv run python scripts/run_transparency_demo.py --entries 17 --checkpoint-every 5
    uv run python scripts/run_transparency_demo.py --prove-index 0

Exit code 0 only if every step passes.

NOTE: `cryptography` is not a dependency of this repo, so without it this demo
signs with HmacDevSigner, which is NOT FOR PRODUCTION -- an HMAC proves nothing
to anybody who does not already hold the secret, and the operator holds it. The
Ed25519 path in src/transparency/checkpoint.py switches on automatically as soon
as the package is importable; this script picks it up with no changes.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# Add project root to path so `src` imports work both as `uv run python scripts/run_transparency_demo.py`
# and `uv run python -m scripts.run_transparency_demo` without PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.transparency.anchoring import OpenTimestampsStubProvider
from src.transparency.checkpoint import (
    HMAC_DEV_ALGORITHM,
    Checkpoint,
    HmacDevSigner,
    Signer,
    build_checkpoint,
    checkpoint_digest,
    ed25519_available,
    generate_ed25519_signer,
    sign_checkpoint,
    verify_checkpoint,
)
from src.transparency.log import InMemoryMerkleLog, verify_chain
from src.transparency.proofs import InclusionProof, inclusion_proof, verify_inclusion

BASE_TIME = datetime(2026, 10, 1, 6, 0, 0, tzinfo=timezone.utc)

SYNTHETIC_SOURCES = [
    ("reuters.com", "tier1"),
    ("apnews.com", "tier1"),
    ("bbc.co.uk", "tier1"),
    ("aljazeera.com", "tier2"),
    ("example-local.blog", "tier3"),
]


def synthetic_observation(index: int) -> dict[str, object]:
    """A stand-in for what ingestion records for a fetched article.

    Raw observation only: bytes as fetched, fetch time, headers, source. No
    story, no cluster, no score -- derived data stays out of the append-only
    layer, because clustering and LLM output change when the code changes.
    """
    domain, tier = SYNTHETIC_SOURCES[index % len(SYNTHETIC_SOURCES)]
    fetched_at = BASE_TIME + timedelta(minutes=7 * index)
    return {
        "body_sha256": f"{index:064x}",  # Stands in for sha256 of the fetched bytes
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
        "http_status": 200,
        "source_domain": domain,
        "source_tier": tier,
        "url": f"https://{domain}/synthetic/{index}",
    }


def build_signer(label: str = "primary") -> Signer:
    """Ed25519 when the optional package is installed, else the dev HMAC.

    `label` keeps the secondary signer genuinely a different key, which is what
    the wrong-key negative control needs.
    """
    if ed25519_available():
        return generate_ed25519_signer(seed=f"transparency-demo-{label}".encode())
    return HmacDevSigner(f"demo-only-not-a-real-secret-{label}".encode(), key_id=f"demo-hmac-{label}")


async def run(entries: int, checkpoint_every: int, prove_index: int) -> int:
    steps: list[tuple[str, bool]] = []

    def step(name: str, passed: bool, detail: str = "") -> None:
        steps.append((name, passed))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    signer = build_signer()
    print("=== Signed Merkle transparency log demo ===")
    print(f"signer: {signer.algorithm} / {signer.key_id}")
    if signer.algorithm == HMAC_DEV_ALGORITHM:
        print("WARNING: HMAC development signer in use. NOT FOR PRODUCTION.")
    print(f"entries: {entries}, checkpoint every {checkpoint_every}, proving index {prove_index}")
    print()

    log = InMemoryMerkleLog()
    for index in range(entries):
        await log.append(synthetic_observation(index))
    head = (await log.head()).hex()
    step(f"appended {entries} observations", await log.size() == entries, f"chain head={head[:16]}")

    step(
        "hash chain verifies from genesis",
        verify_chain(await log.entries()),
        f"{entries} entries, contiguous indices, leaf hashes recomputed",
    )

    print()
    print("--- signed checkpoints + external anchoring ---")
    anchors: list[tuple[int, str]] = []
    anchor_provider = OpenTimestampsStubProvider()
    checkpoints = []
    for size in range(checkpoint_every, entries + 1, checkpoint_every):
        checkpoint = await build_checkpoint(log, size)
        signed = sign_checkpoint(checkpoint, signer)
        checkpoints.append(signed)
        digest = checkpoint_digest(signed)
        attestation = await anchor_provider.anchor(digest)
        anchors.append((size, attestation.digest_hex))
        step(
            f"tree_size={size}: root built, signed, handed to the anchor provider",
            verify_checkpoint(signed, signer) and attestation.digest_hex == digest,
            f"root={checkpoint.merkle_root.hex()[:16]} anchored digest={digest[:16]}",
        )

    covering = [cp for cp in checkpoints if cp.checkpoint.tree_size > prove_index]
    if not covering:
        print(f"ERROR: --prove-index {prove_index} is not covered by any checkpoint. "
              f"Largest checkpoint covers tree_size={checkpoints[-1].checkpoint.tree_size}; "
              f"use --prove-index {checkpoints[-1].checkpoint.tree_size - 1} or a smaller "
              f"--checkpoint-every.")
        return 2
    proving = covering[0]

    entry = await log.get(prove_index)
    if entry is None:
        print(f"ERROR: no entry at index {prove_index}")
        return 2
    proof = await inclusion_proof(log, prove_index, proving.checkpoint.tree_size)

    print()
    print(f"--- inclusion proof: entry {prove_index} in checkpoint tree_size="
          f"{proving.checkpoint.tree_size} ---")
    print(f"entry      : {entry.describe()}")
    print(f"leaf_hash  : {entry.leaf_hash_hex}")
    print(f"root       : {proving.checkpoint.merkle_root.hex()}")
    print(f"chain_hash : {proving.checkpoint.chain_hash.hex()}")
    print(f"proof      : index={proof.index} tree_size={proof.tree_size} siblings={len(proof.siblings)}")
    for level, sibling in enumerate(proof.siblings):
        print(f"  sibling[{level}] = {sibling.hex()}")
    print()

    step("checkpoint signature verifies with the signing key", verify_checkpoint(proving, signer))
    step(
        "inclusion proof folds to the checkpoint root",
        verify_inclusion(entry, proof, proving),
        f"{len(proof.siblings)} sibling(s) recomputed from the leaf hash",
    )

    print()
    print("--- negative controls ---")
    other_checkpoints = [cp for cp in checkpoints if cp is not proving]
    if other_checkpoints:
        foreign = other_checkpoints[0]
        rejected = not verify_inclusion(entry, proof, foreign)
        step(
            f"proof for tree_size={proving.checkpoint.tree_size} rejected by a "
            f"tree_size={foreign.checkpoint.tree_size} checkpoint",
            rejected,
        )
    else:
        empty_proof = InclusionProof(index=prove_index, tree_size=proving.checkpoint.tree_size, siblings=())
        step(
            "proof with no siblings rejected against a non-trivial root",
            not verify_inclusion(entry, empty_proof, proving),
        )

    forged_root = Checkpoint(
        tree_size=proving.checkpoint.tree_size,
        merkle_root=bytes(32),
        chain_hash=proving.checkpoint.chain_hash,
        timestamp=proving.checkpoint.timestamp,
    )
    forged = sign_checkpoint(forged_root, build_signer("impostor"))
    step("checkpoint signed by a different key rejected", not verify_checkpoint(forged, signer))

    tampered = InMemoryMerkleLog()
    for index in range(entries):
        await tampered.append({**synthetic_observation(index), "body_sha256": f"deadbeef{index:056d}"})
    # Self-consistent proof over a rewritten history: it folds to the rewritten
    # log's own root, which is not the root the anchor provider was given.
    tampered_proof = await inclusion_proof(tampered, prove_index, entries)
    tampered_entry = await tampered.get(prove_index)
    assert tampered_entry is not None
    step(
        "self-consistent proof over a rewritten log rejected by the anchored checkpoint",
        not verify_inclusion(tampered_entry, tampered_proof, proving),
    )

    print()
    print("--- anchoring record ---")
    for size, digest in anchors:
        print(f"tree_size={size} -> {digest}")
    print(f"calendar a real submission would use: {anchor_provider.calendar_url}")
    print("stub: nothing was submitted. A real OpenTimestamps attestation plugs in behind")
    print("src/transparency/anchoring.py AnchorProvider.anchor(); see that module docstring.")

    passed = all(ok for _, ok in steps)
    print()
    print(f"RESULT: {'PASS' if passed else 'FAIL'} ({sum(1 for _, ok in steps if ok)}/{len(steps)} steps)")
    return 0 if passed else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="Signed Merkle log demo: build, checkpoint, prove, verify")
    parser.add_argument("--entries", type=int, default=11, help="Observations to append (default: 11)")
    parser.add_argument("--checkpoint-every", type=int, default=5,
                        help="Sign and anchor a checkpoint every N entries (default: 5)")
    parser.add_argument("--prove-index", type=int, default=0,
                        help="Entry to prove (default: 0); needs a checkpoint covering it")
    args = parser.parse_args()

    if args.entries < 1:
        print("ERROR: --entries must be >= 1")
        return 2
    if args.checkpoint_every < 1:
        print("ERROR: --checkpoint-every must be >= 1")
        return 2
    if not 0 <= args.prove_index < args.entries:
        print(f"ERROR: --prove-index must be in [0, {args.entries - 1}]")
        return 2
    return await run(args.entries, args.checkpoint_every, args.prove_index)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))