"""Persistence for signed checkpoints: what a proof permalink anchors to.

The checkpoint math and signing live in checkpoint.py; this module is the
append-only table a published checkpoint lands in, plus the two queries the
public proof page needs:

    save_checkpoint(session, signed)          persist one signed checkpoint
    latest_checkpoint_covering(session, idx)  newest checkpoint whose tree covers idx
    TransparencyQuarantine                     rows the signer must not build on

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

from src.transparency.checkpoint import (
    FORMAT_JSON_V1,
    TREE_SCHEME_CT_V1,
    SignedCheckpoint,
)
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

    tree_scheme arrived with v3 (20261002250000) and is NOT NULL: a row whose
    root was computed by one tree hashing is not comparable with a row computed
    by another, and a nullable scheme would make "old row" and "row from a
    database where the migration has not run" indistinguishable. The default is
    the legacy scheme, so every pre-existing row reads back as what it is.

    entry_timestamps is the hex of the v3 entry-timestamp commitment. It is
    nullable because v1/v2 rows have none, and it MUST be stored: it is inside
    the signed bytes, so a v3 row that lost it on reload would fail its own
    signature verification on the next run.
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
    checkpoint_format = Column(String(32))  # "n1-json-v1", "...-v2" or "...-v3"
    key_name = Column(String(128))  # C2SP signed-note key name (note formats only)
    previous_digest = Column(String(64))  # hex digest of the previous signed checkpoint
    log_id = Column(String(128))  # which log this row covers, matching the note's origin
    tree_scheme = Column(String(32), nullable=False, default=TREE_SCHEME_CT_V1)
    entry_timestamps = Column(String(64))  # hex root over covered (index, timestamp) pairs
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
                # v3 only. Checkpoint.from_dict leaves it None on older formats,
                # where the signed bytes never carried it.
                "entry_timestamps": self.entry_timestamps,
            }
        )


class TransparencyQuarantine(TransparencyBase):
    """Rows an operator has quarantined, so the signer stops building on them.

    Why a table and not a status column: the two transparency tables are
    append-only by trigger (20261002193000) and the signer role holds no UPDATE
    or DELETE at all, which is right -- a signer that can delete an inconvenient
    checkpoint is an equivocation primitive. So exclusion has to be recorded
    somewhere else, and it has to be recorded the same way: appended, never
    removed.

    Two things need quarantining and both are permanent states that would
    otherwise halt the signer forever:

    - A row planted with a huge tree_size. It is append-only, so it can never be
      deleted, and every subsequent run refuses log_shrank against it.
    - A pre-v2 row signed with the development HMAC. No public key can verify it,
      so once F1 authenticates the previous checkpoint it refuses against that
      row too. This is the honest consequence of F1 rather than a bug in it.

    Quarantine cannot be used to lower the bar: the signer skips these rows when
    choosing the previous checkpoint, but the external head does not move, so a
    quarantined genuine head still refuses with head_ahead_of_log. Excluding a row
    and accepting a shorter history are not the same act.

    checkpoint_id is a text id rather than a UUID foreign key, deliberately. An
    FK to transparency_checkpoints(id) would make the quarantine insert fail for
    a row that does not exist -- which is exactly the case where an operator is
    recording a planted id by hand -- and would make the quarantine row itself
    deletable by cascade if the referenced row ever were.
    """

    __tablename__ = "transparency_quarantine"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    checkpoint_id = Column(String(64), nullable=False, unique=True)
    reason = Column(Text, nullable=False, default="")
    quarantined_by = Column(String(128))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


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
        tree_scheme=checkpoint.tree_scheme(),
        entry_timestamps=(
            checkpoint.entry_timestamps.hex() if checkpoint.entry_timestamps else None
        ),
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
