"""Inclusion proofs: "this observation was already in the tree at time T".

This is the permalink payload. Given an entry, a sibling path, and a signed
checkpoint, anybody can recompute the root and see whether it matches -- no
access to the log, the database, or the operator required.

Scope note: verify_inclusion() checks the mathematics against the checkpoint's
root and tree_size. It does not check the checkpoint's signature. Those are two
independent facts and both are needed:

    verify_checkpoint(signed, verifier)   -> the operator signed this root
    verify_inclusion(entry, proof, signed) -> this entry is in that root

A permalink should carry all three: the entry, the proof, and the signed
checkpoint with its key id.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.transparency.checkpoint import (
    NODE_PREFIX,
    Checkpoint,
    SignedCheckpoint,
    Signer,
    merkle_levels,
    tree_depth,
    verify_checkpoint,
)
from src.transparency.log import LogEntry, MerkleLog, canonical_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InclusionProof:
    """Sibling hashes from a leaf up to the root, plus the position they apply to.

    Direction is implied by `index`, so it does not need to travel with the
    proof: at each level, an even position is the left child.
    """
    index: int
    tree_size: int
    siblings: tuple[bytes, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "siblings": [sibling.hex() for sibling in self.siblings],
            "tree_size": self.tree_size,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InclusionProof:
        return cls(
            index=int(data["index"]),
            tree_size=int(data["tree_size"]),
            siblings=tuple(bytes.fromhex(str(s)) for s in data["siblings"]),
        )


@dataclass(frozen=True)
class ProofStep:
    """One level of the leaf-to-root fold, recorded so a permalink page can
    show every intermediate digest. `left` and `right` are the two children in
    hash order; digest = sha256(NODE_PREFIX || left || right)."""
    level: int
    position: int
    left: bytes
    right: bytes
    digest: bytes


def proof_steps(leaf: bytes, index: int, siblings: Sequence[bytes]) -> list[ProofStep]:
    """Fold a leaf and its sibling path, recording each level.

    The even/odd ordering rule lives here and only here: an even position
    hashes as the left child (0x01 || node || sibling), an odd position as the
    right child (0x01 || sibling || node). proof_root() delegates to this so
    the page's displayed steps and the verified root can never disagree.
    """
    steps: list[ProofStep] = []
    digest = leaf
    position = index
    for level, sibling in enumerate(siblings):
        if position % 2 == 0:
            left, right = digest, sibling
        else:
            left, right = sibling, digest
        digest = hashlib.sha256(NODE_PREFIX + left + right).digest()
        steps.append(ProofStep(level=level, position=position, left=left, right=right, digest=digest))
        position //= 2
    return steps


def proof_root(leaf: bytes, index: int, siblings: Sequence[bytes]) -> bytes:
    """Fold a leaf and its sibling path up to a root.

    Implemented through proof_steps() so the permalink page can display the
    exact intermediate digests this fold produces.
    """
    steps = proof_steps(leaf, index, siblings)
    return steps[-1].digest if steps else leaf


async def inclusion_proof(log: MerkleLog, index: int, checkpoint_size: int) -> InclusionProof:
    """Sibling path for `index` in a tree of the first `checkpoint_size` entries.

    `checkpoint_size` is the checkpoint's tree_size, not the log's length: a
    proof has to be against the exact tree that was signed, or the fold lands on
    a different root. `checkpoint_size` may therefore be less than the log.
    """
    if index < 0:
        raise ValueError(f"index must be >= 0, got {index}")
    if checkpoint_size <= 0:
        raise ValueError(f"checkpoint_size must be >= 1, got {checkpoint_size}")
    if index >= checkpoint_size:
        raise ValueError(f"index {index} is outside a tree of size {checkpoint_size}")

    levels = merkle_levels((await log.leaf_hashes())[:checkpoint_size])
    siblings: list[bytes] = []
    position = index
    for level in levels[:-1]:
        sibling_position = position ^ 1
        if sibling_position >= len(level):
            sibling_position = position  # duplicated final node of an odd level
        siblings.append(level[sibling_position])
        position //= 2
    return InclusionProof(index=index, tree_size=checkpoint_size, siblings=tuple(siblings))


def verify_inclusion(
    entry: LogEntry,
    proof: InclusionProof,
    checkpoint: Checkpoint | SignedCheckpoint,
) -> bool:
    """True iff `entry` hashes into `checkpoint`'s root.
    Deliberately cheap to feed wrong data: everything is a False, nothing
    raises, because a proof is untrusted input from a public permalink.
    """
    body: Checkpoint = checkpoint.checkpoint if isinstance(checkpoint, SignedCheckpoint) else checkpoint

    if proof.tree_size != body.tree_size:
        logger.info(f"Proof is for tree_size {proof.tree_size}, checkpoint covers {body.tree_size}")
        return False
    if proof.index != entry.index:
        logger.info(f"Proof is for index {proof.index}, entry is index {entry.index}")
        return False
    if entry.index >= body.tree_size:
        return False
    if len(proof.siblings) != tree_depth(body.tree_size):
        logger.info(
            f"Proof has {len(proof.siblings)} siblings, depth of size {body.tree_size} "
            f"is {tree_depth(body.tree_size)}"
        )
        return False

    leaf = entry.leaf_hash
    try:
        if hashlib.sha256(canonical_json(entry.payload)).digest() != leaf:
            logger.info(f"Entry {entry.index}: payload does not match its recorded leaf hash")
            return False
    except TypeError:
        logger.info(f"Entry {entry.index}: payload is not canonicalizable")
        return False

    return proof_root(leaf, proof.index, proof.siblings) == body.merkle_root


async def verify_entry(
    log: MerkleLog,
    index: int,
    signed: SignedCheckpoint,
    signer: Signer,
) -> bool:
    """Chain the two checks for a permalink: signature first, then inclusion.

    Returns the inclusion verdict; a bad signature short-circuits to False,
    because an unverifiable checkpoint must not be used as evidence.
    """
    if not verify_checkpoint(signed, signer):
        logger.info(f"Refusing to verify entry {index}: checkpoint signature does not check out")
        return False
    entry = await log.get(index)
    if entry is None:
        return False
    proof = await inclusion_proof(log, index, signed.checkpoint.tree_size)
    return verify_inclusion(entry, proof, signed)