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
    LEAF_PREFIX,
    NODE_PREFIX,
    TREE_SCHEME_CT_V1,
    TREE_SCHEME_RFC6962,
    Checkpoint,
    SignedCheckpoint,
    Signer,
    entry_timestamps_root,
    merkle_levels,
    proof_length_for_scheme,
    rfc6962_path,
    verify_checkpoint,
)
from src.transparency.log import LogEntry, MerkleLog, canonical_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InclusionProof:
    """Sibling hashes from a leaf up to the root, plus the position they apply to.

    Direction is implied by `index`, so it does not need to travel with the
    proof: at each level, an even position is the left child.

    `scheme` says which tree the siblings came from. It is part of the proof
    because the two schemes fold differently -- RFC 6962 prefixes the leaf and
    does not duplicate the odd node, so an old path folded against a v3 root (or
    the reverse) would silently produce a different root rather than an error.
    It defaults to the legacy scheme so a proof published before v3 still reads.
    """
    index: int
    tree_size: int
    siblings: tuple[bytes, ...]
    scheme: str = TREE_SCHEME_CT_V1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "siblings": [sibling.hex() for sibling in self.siblings],
            "tree_size": self.tree_size,
            "scheme": self.scheme,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InclusionProof:
        return cls(
            index=int(data["index"]),
            tree_size=int(data["tree_size"]),
            siblings=tuple(bytes.fromhex(str(s)) for s in data["siblings"]),
            scheme=str(data.get("scheme") or TREE_SCHEME_CT_V1),
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


def proof_steps(
    leaf: bytes,
    index: int,
    siblings: Sequence[bytes],
    scheme: str = TREE_SCHEME_CT_V1,
) -> list[ProofStep]:
    """Fold a leaf and its sibling path, recording each level.

    The even/odd ordering rule lives here and only here: an even position
    hashes as the left child (0x01 || node || sibling), an odd position as the
    right child (0x01 || sibling || node). proof_root() delegates to this so
    the page's displayed steps and the verified root can never disagree.

    Under RFC 6962 the leaf is domain-separated first (0x00 || leaf) and the
    odd-node duplication is gone, so the first step hashes a prefixed leaf and
    the path is whatever length the position needs.
    """
    if scheme not in (TREE_SCHEME_CT_V1, TREE_SCHEME_RFC6962):
        raise ValueError(f"unknown tree scheme {scheme!r}")
    steps: list[ProofStep] = []
    digest = hashlib.sha256(LEAF_PREFIX + leaf).digest() if scheme == TREE_SCHEME_RFC6962 else leaf
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


def proof_root(
    leaf: bytes,
    index: int,
    siblings: Sequence[bytes],
    scheme: str = TREE_SCHEME_CT_V1,
) -> bytes:
    """Fold a leaf and its sibling path up to a root.

    Implemented through proof_steps() so the permalink page can display the
    exact intermediate digests this fold produces.
    """
    if scheme == TREE_SCHEME_RFC6962 and not siblings:
        # A one-leaf tree: the root is the leaf hash, still domain-separated.
        return hashlib.sha256(LEAF_PREFIX + leaf).digest()
    steps = proof_steps(leaf, index, siblings, scheme)
    return steps[-1].digest if steps else leaf


async def inclusion_proof(
    log: MerkleLog,
    index: int,
    checkpoint_size: int,
    *,
    scheme: str = TREE_SCHEME_CT_V1,
) -> InclusionProof:
    """Sibling path for `index` in a tree of the first `checkpoint_size` entries.

    `checkpoint_size` is the checkpoint's tree_size, not the log's length: a
    proof has to be against the exact tree that was signed, or the fold lands on
    a different root. `checkpoint_size` may therefore be less than the log.

    `scheme` must be the scheme of the checkpoint the proof will be verified
    against; it defaults to the legacy CT tree for callers that have not moved
    yet.
    """
    if index < 0:
        raise ValueError(f"index must be >= 0, got {index}")
    if checkpoint_size <= 0:
        raise ValueError(f"checkpoint_size must be >= 1, got {checkpoint_size}")
    if index >= checkpoint_size:
        raise ValueError(f"index {index} is outside a tree of size {checkpoint_size}")

    leaves = list((await log.leaf_hashes())[:checkpoint_size])
    if scheme == TREE_SCHEME_RFC6962:
        siblings = rfc6962_path(leaves, index)
    elif scheme == TREE_SCHEME_CT_V1:
        levels = merkle_levels(leaves)
        siblings = []
        position = index
        for level in levels[:-1]:
            sibling_position = position ^ 1
            if sibling_position >= len(level):
                sibling_position = position  # duplicated final node of an odd level
            siblings.append(level[sibling_position])
            position //= 2
    else:
        raise ValueError(f"unknown tree scheme {scheme!r}")
    return InclusionProof(
        index=index, tree_size=checkpoint_size, siblings=tuple(siblings), scheme=scheme
    )



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

    try:
        scheme = body.tree_scheme()
    except ValueError:
        logger.info(f"Checkpoint carries an unknown format: {body.format!r}")
        return False
    if proof.scheme != scheme:
        # A path built for one tree must not be folded against another's root.
        logger.info(f"Proof is for scheme {proof.scheme!r}, checkpoint is {scheme!r}")
        return False
    if proof.tree_size != body.tree_size:
        logger.info(f"Proof is for tree_size {proof.tree_size}, checkpoint covers {body.tree_size}")
        return False
    if proof.index != entry.index:
        logger.info(f"Proof is for index {proof.index}, entry is index {entry.index}")
        return False
    if entry.index >= body.tree_size:
        return False
    # RFC 6962 paths are not a fixed depth: a promoted odd node has no sibling,
    # so the expected length depends on the position, not just the size.
    expected = proof_length_for_scheme(entry.index, body.tree_size, scheme)
    if len(proof.siblings) != expected:
        logger.info(
            f"Proof has {len(proof.siblings)} siblings, {scheme} needs {expected} "
            f"for index {entry.index} of {body.tree_size}"
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

    return proof_root(leaf, proof.index, proof.siblings, scheme) == body.merkle_root



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
    proof = await inclusion_proof(
        log, index, signed.checkpoint.tree_size, scheme=signed.checkpoint.tree_scheme()
    )
    return verify_inclusion(entry, proof, signed)

def verify_entry_timestamps(
    signed: SignedCheckpoint,
    entries: Sequence[LogEntry],
) -> bool:
    """Check a v3 checkpoint's entry-timestamp commitment against the entries.

    Returns True when the checkpoint does not claim a timestamp commitment (v1
    and v2 rows, which cannot be checked this way -- that residual is exactly why
    v3 exists). Otherwise it recomputes the root over the first
    `checkpoint.tree_size` entries and compares. An entry whose timestamp was
    edited after signing fails here even though its leaf hash, chain hash and
    signature are all untouched, which is the hole F15 closes.

    This needs the covered prefix, not a single leaf: an audit path proves
    inclusion of a leaf hash, and the timestamp commitment is over the whole
    prefix. Callers that only have one entry must treat the timestamp claim as
    unchecked rather than as verified.
    """
    body = signed.checkpoint
    claimed = body.entry_timestamps
    if claimed is None:
        return True
    covered = list(entries)[: body.tree_size]
    if len(covered) < body.tree_size:
        logger.info(
            f"Cannot check the entry-timestamp commitment: got {len(covered)} entries "
            f"for a checkpoint covering {body.tree_size}"
        )
        return False
    return entry_timestamps_root(covered) == claimed
