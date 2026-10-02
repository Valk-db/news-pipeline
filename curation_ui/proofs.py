"""Public inclusion-proof permalinks: the server-rendered view model.

The route lives in curation_ui.main (/proof/{article_id}); this module builds
the view. Read-only: nothing here writes to the session.

States, all rendered honestly:
- verified: entry found, a checkpoint covers it, the math checks out.
- failed: entry and checkpoint exist but the math does not check out
  (tampered payload, wrong siblings, checkpoint mismatch). Shown, not hidden.
- pending_unstamped: article.log_index is NULL, never stamped.
- pending_no_entry: stamped index is beyond the log's current length.
- pending_no_checkpoint: stamped, but no checkpoint covers the index yet.
- pending_log_missing: the transparency tables are not provisioned in this
  database yet (20261001000700_merkle_log_entries.sql and
  20261002000100_proof_permalinks.sql have not been applied).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.exc import OperationalError, ProgrammingError

from src.schema.models import RawArticle
from src.transparency.log import SqlAlchemyMerkleLog, canonical_json
from src.transparency.proofs import inclusion_proof, proof_root, proof_steps, verify_inclusion
from src.transparency.store import latest_checkpoint_covering

logger = logging.getLogger(__name__)

# Same markers as src/ingestion/rss_evidence._is_missing_table: the one
# expected, recoverable condition is a table this deployment has not migrated
# yet. Anything else is a real fault and propagates.
_MISSING_TABLE_MARKERS = (
    "does not exist",
    "undefinedtable",
    "undefined table",
    "no such table",
)


def _is_missing_table(exc: BaseException) -> bool:
    if not isinstance(exc, (ProgrammingError, OperationalError)):
        return False
    text = str(getattr(exc, "orig", None) or exc).lower()
    return any(marker in text for marker in _MISSING_TABLE_MARKERS)


@dataclass
class ProofCheck:
    """One named verification step for the page's checklist."""
    label: str
    passed: bool | None  # None = not applicable / could not be checked
    detail: str


@dataclass
class ProofView:
    article_id: str
    title: str
    url: str
    source_domain: str
    source_tier: str | None
    published_at: datetime | None
    fetched_at: datetime | None
    content_hash: str | None
    state: str
    state_headline: str
    state_detail: str
    checks: list[ProofCheck] = field(default_factory=list)
    entry: dict[str, Any] | None = None
    proof: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    checkpoint: dict[str, Any] | None = None


def _article_identity(article: RawArticle) -> dict[str, Any]:
    tier = article.source_tier
    return {
        "article_id": str(article.id),
        "title": article.title or "Untitled article",
        "url": article.url,
        "source_domain": article.source_domain,
        "source_tier": tier.value if tier is not None else None,
        "published_at": article.published_at,
        "fetched_at": article.fetched_at,
        "content_hash": article.content_hash,
    }


def _pending_view(article: RawArticle, state: str, headline: str, detail: str) -> ProofView:
    return ProofView(
        **_article_identity(article),
        state=state,
        state_headline=headline,
        state_detail=detail,
        checks=[ProofCheck(
            label="Article stamped into the transparency log",
            passed=False if state == "pending_unstamped" else None,
            detail=detail,
        )],
    )


def _describe_step(step_index: int, position: int) -> str:
    side = "left child" if position % 2 == 0 else "right child"
    order = "node || sibling" if position % 2 == 0 else "sibling || node"
    return (
        f"Level {step_index}: position {position} is {side}, "
        f"so digest = sha256(0x01 || {order})"
    )


async def build_proof_view(session, article: RawArticle) -> ProofView:
    """Assemble the permalink view for one article. Never raises for proof
    problems: tampering and missing data are states, not exceptions."""
    identity = _article_identity(article)

    if article.log_index is None:
        return _pending_view(
            article,
            "pending_unstamped",
            "Proof pending: this article has not been stamped yet",
            "This article was archived before (or without) the transparency log "
            "stamp. There is no log entry for it, so there is no inclusion proof "
            "to show. Nothing here is fabricated: the proof appears once the "
            "evidence locker stamps the article.",
        )

    log = SqlAlchemyMerkleLog(session)
    try:
        entry = await log.get(article.log_index)
    except Exception as exc:
        if _is_missing_table(exc):
            return _pending_view(
                article,
                "pending_log_missing",
                "Proof pending: the transparency log is not provisioned yet",
                "The merkle_log_entries table does not exist in this database. "
                "Apply 20261001000700_merkle_log_entries.sql and "
                "20261002000100_proof_permalinks.sql (scripts/migrate.py), then "
                "re-run the evidence locker.",
            )
        raise

    if entry is None:
        return _pending_view(
            article,
            "pending_no_entry",
            "Proof pending: log entry not found",
            f"This article claims log entry #{article.log_index}, but the log "
            f"holds fewer entries. The stamp may have been recorded against a "
            f"different log, or the log was reset.",
        )

    try:
        signed = await latest_checkpoint_covering(session, entry.index)
    except Exception as exc:
        if _is_missing_table(exc):
            signed = None
        else:
            raise

    if signed is None:
        view = _pending_view(
            article,
            "pending_no_checkpoint",
            "Proof pending: stamped, not yet checkpointed",
            f"This article is log entry #{entry.index}, but no published "
            f"checkpoint covers it yet. The inclusion proof becomes verifiable "
            f"once the operator signs a checkpoint over a tree containing it.",
        )
        view.entry = {
            "index": entry.index,
            "timestamp": entry.timestamp,
            "leaf_hash": entry.leaf_hash_hex,
            "chain_hash": entry.chain_hash_hex,
            "canonical_payload": entry.canonical_payload.decode("utf-8", errors="replace"),
        }
        view.checks.append(ProofCheck(
            label="Article stamped into the transparency log",
            passed=True,
            detail=f"log entry #{entry.index}, leaf {entry.leaf_hash_hex[:16]}...",
        ))
        return view

    proof = await inclusion_proof(log, entry.index, signed.checkpoint.tree_size)
    ok = verify_inclusion(entry, proof, signed)

    # Explanatory breakdown of the two halves verify_inclusion checks.
    try:
        leaf_recomputed = hashlib.sha256(canonical_json(entry.payload)).digest() == entry.leaf_hash
    except TypeError:
        leaf_recomputed = False
    root_recomputed = proof_root(entry.leaf_hash, proof.index, proof.siblings) == signed.checkpoint.merkle_root

    steps = [
        {
            "level": step.level,
            "position": step.position,
            "left": step.left.hex(),
            "right": step.right.hex(),
            "digest": step.digest.hex(),
            "note": _describe_step(step.level, step.position),
        }
        for step in proof_steps(entry.leaf_hash, proof.index, proof.siblings)
    ]

    checkpoint = signed.checkpoint
    return ProofView(
        **identity,
        state="verified" if ok else "failed",
        state_headline=(
            "Inclusion verified: this article is in the signed log"
            if ok else
            "Verification failed: this proof does not check out"
        ),
        state_detail=(
            f"Log entry #{entry.index} folds through {len(proof.siblings)} sibling "
            f"hashes to the checkpoint root, and the checkpoint is signed by "
            f"{signed.key_id}."
            if ok else
            "The recomputed root does not match the signed checkpoint root, or "
            "the entry's payload does not match its recorded leaf hash. Treat "
            "this article's archive claim as unproven until the mismatch is "
            "explained."
        ),
        checks=[
            ProofCheck(
                label="Article stamped into the transparency log",
                passed=True,
                detail=f"log entry #{entry.index}, leaf {entry.leaf_hash_hex[:16]}...",
            ),
            ProofCheck(
                label="Payload hashes to the recorded leaf hash",
                passed=leaf_recomputed,
                detail=(
                    "sha256 over the canonical payload bytes reproduces the leaf"
                    if leaf_recomputed else
                    "the payload no longer hashes to its leaf: the entry was altered"
                ),
            ),
            ProofCheck(
                label="Sibling hashes fold to the checkpoint root",
                passed=root_recomputed,
                detail=(
                    f"{len(proof.siblings)} levels fold to {checkpoint.merkle_root.hex()[:16]}..."
                    if root_recomputed else
                    "the fold lands on a different root than the checkpoint signs"
                ),
            ),
            ProofCheck(
                label="Checkpoint signature on file",
                passed=None,
                detail=(
                    f"{signed.algorithm}, key {signed.key_id}. Verify it against "
                    f"the operator's published public key using the signing bytes "
                    f"below; this page does not hold the key."
                ),
            ),
        ],
        entry={
            "index": entry.index,
            "timestamp": entry.timestamp,
            "leaf_hash": entry.leaf_hash_hex,
            "chain_hash": entry.chain_hash_hex,
            "canonical_payload": entry.canonical_payload.decode("utf-8", errors="replace"),
            "payload": entry.payload,
        },
        proof={
            "index": proof.index,
            "tree_size": proof.tree_size,
            "siblings": [s.hex() for s in proof.siblings],
        },
        steps=steps,
        checkpoint={
            "tree_size": checkpoint.tree_size,
            "merkle_root": checkpoint.merkle_root.hex(),
            "chain_hash": checkpoint.chain_hash.hex(),
            "timestamp": checkpoint.timestamp,
            "algorithm": signed.algorithm,
            "key_id": signed.key_id,
            "signature": signed.signature.hex(),
            "signing_bytes": signed.signing_bytes().decode("ascii", errors="replace"),
        },
    )
