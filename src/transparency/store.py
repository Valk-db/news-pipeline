"""Persistence for signed checkpoints: what a proof permalink anchors to.

The checkpoint math and signing live in checkpoint.py; this module is the
append-only table a published checkpoint lands in, plus the two queries the
public proof page needs:

    save_checkpoint(session, signed)          persist one signed checkpoint
    latest_checkpoint_covering(session, idx)  newest checkpoint whose tree covers idx

The model sits on TransparencyBase (see log.py), deliberately NOT on the app
Base: keeping the log's tables out of Base keeps them out of
scripts/check_schema.py's drift comparison. DDL:
supabase/migrations/20261002000100_proof_permalinks.sql, generated from this
model with the postgres dialect.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, desc, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from src.transparency.checkpoint import FORMAT_JSON_V1, SignedCheckpoint
from src.transparency.log import TransparencyBase, _utc

logger = logging.getLogger(__name__)


class TransparencyCheckpoint(TransparencyBase):
    """One published signed checkpoint. Append-only: no update path exists.

    A permalink anchors its proof to the newest checkpoint covering the
    entry's index, so readers never need to trust the operator's current
    head -- only a checkpoint the operator signed and published.

    checkpoint_format, key_name and previous_digest arrived with the v2 signer
    (20261002200000). All three are nullable so the rows signed under v1 keep
    loading and keep verifying byte-for-byte: a row with no format IS v1, which
    is why to_signed() defaults the format rather than requiring the column.
    """

    __tablename__ = "transparency_checkpoints"
    __table_args__ = (
        Index("ix_transparency_checkpoints_tree_size", "tree_size"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tree_size = Column(Integer, nullable=False)  # entries covered: [0, tree_size)
    merkle_root = Column(String(64), nullable=False)  # hex of the root over those entries
    chain_hash = Column(String(64), nullable=False)  # hex of the last covered entry's chain hash
    timestamp = Column(DateTime(timezone=True), nullable=False)  # when the operator signed
    signature = Column(Text, nullable=False)  # hex of the detached signature
    algorithm = Column(String(64), nullable=False)  # e.g. "ed25519", "hmac-sha256-dev"
    key_id = Column(String(128), nullable=False)
    checkpoint_format = Column(String(32))  # "n1-json-v1" (v1) or "c2sp-tlog-checkpoint-v2"
    key_name = Column(String(128))  # C2SP signed-note key name (v2 only)
    previous_digest = Column(String(64))  # hex digest of the previous signed checkpoint
    log_id = Column(String(128))  # which log this row covers, matching the note's origin
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    def to_signed(self) -> SignedCheckpoint:
        return SignedCheckpoint.from_dict(
            {
                "tree_size": self.tree_size,
                "merkle_root": self.merkle_root,
                "chain_hash": self.chain_hash,
                "timestamp": _utc(self.timestamp).isoformat().replace("+00:00", "Z"),
                "signature": self.signature,
                "algorithm": self.algorithm,
                "key_id": self.key_id,
                # Absent on v1 rows: a row with no format value is v1, which is
                # what Checkpoint.from_dict defaults to.
                "format": self.checkpoint_format,
                "key_name": self.key_name,
                "previous_digest": self.previous_digest,
                "origin": self.log_id,
            }
        )


async def save_checkpoint(session: AsyncSession, signed: SignedCheckpoint) -> TransparencyCheckpoint:
    """Persist a signed checkpoint. Appends; never updates or deletes."""
    checkpoint = signed.checkpoint
    row = TransparencyCheckpoint(
        tree_size=checkpoint.tree_size,
        merkle_root=checkpoint.merkle_root.hex(),
        chain_hash=checkpoint.chain_hash.hex(),
        timestamp=checkpoint.timestamp,
        signature=signed.signature.hex(),
        algorithm=signed.algorithm,
        key_id=signed.key_id,
        checkpoint_format=checkpoint.format,
        key_name=signed.key_name,
        previous_digest=checkpoint.previous_digest.hex() if checkpoint.previous_digest else None,
        log_id=checkpoint.origin if checkpoint.format != FORMAT_JSON_V1 else None,
    )
    session.add(row)
    await session.flush()
    return row


async def latest_checkpoint_covering(session: AsyncSession, index: int) -> SignedCheckpoint | None:
    """Newest checkpoint whose tree covers `index` (tree_size > index).

    Newest wins: if the operator published several checkpoints, the proof
    anchors to the latest one that still covers the entry, so the permalink
    shows the freshest signed root available for it.
    """
    candidates = await checkpoints_covering(session, index)
    return candidates[0] if candidates else None


async def checkpoints_covering(session: AsyncSession, index: int) -> list[SignedCheckpoint]:
    """Every checkpoint whose tree covers `index`, newest first.

    The proof view walks these newest-first and anchors to the first whose
    signature verifies against a published operator key, so a forged row an
    attacker slipped in (newer, self-consistent, but unsigned by any known
    key) cannot displace the genuine checkpoint: it degrades that candidate
    to the honest "unverified signature" state instead of hijacking the page.
    """
    rows = await session.execute(
        select(TransparencyCheckpoint)
        .where(TransparencyCheckpoint.tree_size > index)
        .order_by(desc(TransparencyCheckpoint.tree_size), desc(TransparencyCheckpoint.created_at))
    )
    return [row.to_signed() for row in rows.scalars().all()]
