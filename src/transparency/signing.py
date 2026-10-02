"""Signing service: the append-only enforcement the signer itself has to provide.

The database refuses UPDATE and DELETE on the transparency tables (see
supabase/migrations/20261002193000_transparency_append_only.sql), so a signed
history cannot be edited in place. That is necessary and not sufficient: it
stops mutation of rows that already exist, but nothing stops the *signer* from
publishing a second, different checkpoint at the same tree size, or from
jumping back to a smaller size and signing a shorter history that looks
perfectly consistent with itself. Both are split-brain attacks on the one thing
a transparency log exists to prevent.

So every signing run, in this order, before it signs anything:

1. Takes a transaction-scoped advisory lock, so two overlapping cron fires (or
   a retry racing the original) cannot both sign. Non-blocking: the loser skips
   this run rather than queueing, because a stale run must never sign late.
2. Fixes tree_size from the log's current size, once. A tree size that moved
   mid-run would produce a root over a different set of leaves than the one
   that gets reported.
3. Refuses to go backwards. A new checkpoint must cover at least as many entries
   as the newest published one.
4. Re-derives the root over the first N leaves and compares it against the
   newest published checkpoint's root at the same size. tlog-checkpoint requires
   exactly this: "logs MUST not sign any checkpoint which is inconsistent with
   any checkpoint it previously signed". We cannot ship an RFC 6962 consistency
   proof for our tree shape (see checkpoint.py), so re-derivation is how the
   guarantee is made real.
5. Refuses to sign a tree size that is already published with a different root
   (equivocation), and is idempotent when a v2 checkpoint is already published
   there with the same one. Idempotency is per signed format, not per tree size:
   a v1 row at that size cannot be rewritten and does not count as a v2
   checkpoint, so the first v2 checkpoint is published at the same size as the
   v1 one it follows and the two are chained by previous_digest.

Refusals are not silent: each returns a refusal with a reason code and is
written to the transparency_alerts table by the caller, which is what
/healthz/details reads. A signer that quietly does nothing is indistinguishable
from a signer that is working.

This module is HTTP-free on purpose. curation_ui/cron.py is the transport;
everything here can be called from a script or a test with a session and a
signer, and the cron route can be tested without a database or a key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.transparency.checkpoint import (
    FORMAT_C2SP_V2,
    FORMAT_JSON_V1,
    SignedCheckpoint,
    Signer,
    build_checkpoint,
    checkpoint_digest,
    merkle_root,
    sign_checkpoint,
)
from src.transparency.log import MerkleLog
from src.transparency.store import TransparencyCheckpoint, save_checkpoint

logger = logging.getLogger(__name__)

# Advisory lock key for the signing transaction. Any constant works as long as
# only this code path uses it; it is derived from a string so it reads as a name
# rather than as a magic number.
ADVISORY_LOCK_KEY = 0x5452414E53504152  # "TRANSPAR"

# Refusal reason codes. These are strings, not exceptions: a refusal is a
# result the operator has to be able to read in an alert table.
REFUSAL_LOCK_HELD = "lock_held"
REFUSAL_EMPTY_LOG = "empty_log"
REFUSAL_LOG_SMALLER = "log_shrank"
REFUSAL_INCONSISTENT_HISTORY = "inconsistent_history"
REFUSAL_EQUIVOCATION = "equivocating_checkpoint"
REFUSAL_ALREADY_SIGNED = "already_signed"


@dataclass(frozen=True)
class SigningResult:
    """What one signing run did. `signed` is the only field callers act on."""

    signed: SignedCheckpoint | None
    status: str
    reason: str | None = None
    tree_size: int = 0
    merkle_root: str = ""
    previous_digest: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "signed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "tree_size": self.tree_size,
            "merkle_root": self.merkle_root,
            "previous_digest": self.previous_digest,
            "detail": self.detail,
        }


def _refusal(reason: str, **detail: Any) -> SigningResult:
    return SigningResult(signed=None, status="refused", reason=reason, detail=detail)


async def _try_advisory_lock(session: AsyncSession) -> bool:
    """Take the transaction-scoped signing lock, non-blocking.

    False means another signing run holds it, and this run must skip. The lock
    is transaction-scoped on purpose: if this run's transaction rolls back for
    any reason, the lock is released with it, so a crashed run cannot wedge the
    signer forever.
    """
    result = await session.execute(
        text("select pg_try_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
    )
    return bool(result.scalar())


def _row_format(row: TransparencyCheckpoint) -> str:
    """The signed format of a stored row. A null column means v1.

    The column arrived with the v2 signer and is nullable so v1 rows keep
    loading; a row with no format recorded predates v2 and is v1 by definition.
    """
    return row.checkpoint_format or FORMAT_JSON_V1


async def _newest_checkpoint(session: AsyncSession) -> TransparencyCheckpoint | None:
    """The published checkpoint covering the most entries, if any.

    Tie-broken by created_at, newest row first. The tie is real now: a v2
    checkpoint is published at the same tree size as the v1 checkpoint it
    replaces, because v1 rows are append-only and cannot be rewritten. Ordering
    by insertion time is what makes the chain walk forward through that pair
    instead of picking between them arbitrarily.
    """
    rows = await session.execute(
        select(TransparencyCheckpoint)
        .order_by(
            TransparencyCheckpoint.tree_size.desc(),
            TransparencyCheckpoint.created_at.desc(),
        )
        .limit(1)
    )
    return rows.scalars().first()


async def _checkpoints_at_size(session: AsyncSession, tree_size: int) -> list[TransparencyCheckpoint]:
    """Every published checkpoint at exactly this tree size.

    All of them, not just the first: the migration adds UNIQUE(tree_size), but a
    database predating it can hold several rows at one size, and two rows with
    different roots at the same size is precisely the equivocation this function
    exists to catch. Reading only the first would let the second hide.
    """
    rows = await session.execute(
        select(TransparencyCheckpoint).where(TransparencyCheckpoint.tree_size == tree_size)
    )
    return list(rows.scalars().all())


async def sign_next_checkpoint(
    session: AsyncSession,
    log: MerkleLog,
    signer: Signer,
    *,
    origin: str,
    timestamp: datetime | None = None,
    lock: bool = True,
) -> SigningResult:
    """Sign a checkpoint over the whole log, or refuse with a reason.

    `origin` is the log identity written into a v2 note and required by
    tlog-checkpoint; it is a parameter rather than a constant so a test can use
    its own and so a future second log is a call site change, not a code edit.
    """
    if lock and not await _try_advisory_lock(session):
        logger.info("Another checkpoint signing run holds the lock; skipping this one")
        return _refusal(REFUSAL_LOCK_HELD)

    # Fixed once, up front: the reported tree size, the root, and the stored row
    # must all describe the same set of leaves.
    tree_size = await log.size()
    if tree_size <= 0:
        logger.info("Log is empty; nothing to checkpoint")
        return _refusal(REFUSAL_EMPTY_LOG, tree_size=tree_size)

    newest = await _newest_checkpoint(session)
    previous: SignedCheckpoint | None = newest.to_signed() if newest is not None else None
    previous_digest = checkpoint_digest(previous) if previous is not None else None

    if newest is not None and tree_size < newest.tree_size:
        logger.warning(
            f"Log has {tree_size} entries but a checkpoint covering {newest.tree_size} is "
            "already published; refusing to sign a shorter history"
        )
        return _refusal(
            REFUSAL_LOG_SMALLER,
            tree_size=tree_size,
            published_tree_size=newest.tree_size,
        )

    # Re-derive rather than trust: read the leaves back and recompute the root
    # over the first tree_size of them. A checkpoint whose root disagrees with
    # the log as it stands now is a checkpoint of a history that no longer
    # exists, and tlog-checkpoint forbids signing one.
    entries = await log.entries()
    leaves = [entry.leaf_hash for entry in entries[:tree_size]]
    derived_root = merkle_root(leaves)
    derived_hex = derived_root.hex()

    published_at_size = await _checkpoints_at_size(session, tree_size)
    if published_at_size:
        disagreeing = [row for row in published_at_size if row.merkle_root != derived_hex]
        if disagreeing:
            logger.error(
                f"REFUSING: tree_size {tree_size} is already published with root "
                f"{disagreeing[0].merkle_root[:12]} but the log now derives {derived_hex[:12]}. "
                "This is equivocation and must not be signed."
            )
            return _refusal(
                REFUSAL_EQUIVOCATION,
                tree_size=tree_size,
                published_root=disagreeing[0].merkle_root,
                derived_root=derived_hex,
                published_key_id=disagreeing[0].key_id,
            )
        # Idempotency is scoped to the format. A v1 row at this size agreeing
        # with the log does NOT mean there is a v2 checkpoint here: v1 rows are
        # append-only and cannot be rewritten into the new format, so the first
        # v2 checkpoint is published at the same size as the v1 one it follows,
        # and the pair is chained by previous_digest. Treating the v1 row as
        # already-signed would leave the log permanently on HMAC signatures
        # while the cron reports success on every run.
        same_format = [row for row in published_at_size if _row_format(row) == FORMAT_C2SP_V2]
        if same_format:
            logger.info(f"Checkpoint for tree_size {tree_size} is already published and matches")
            return SigningResult(
                signed=same_format[0].to_signed(),
                status=REFUSAL_ALREADY_SIGNED,
                tree_size=tree_size,
                merkle_root=derived_hex,
                previous_digest=previous_digest,
                detail={"checkpoint_id": str(same_format[0].id)},
            )

    if newest is not None and newest.tree_size < tree_size:
        # Re-derive the root over the entries the newest checkpoint already
        # covers. If that no longer matches what was published, the prefix of
        # the log changed under a signed checkpoint, and tlog-checkpoint forbids
        # signing anything on top of that.
        covered = [entry.leaf_hash for entry in entries[: newest.tree_size]]
        rederived_previous = merkle_root(covered).hex()
        if rederived_previous != newest.merkle_root:
            logger.error(
                f"REFUSING: root over the first {newest.tree_size} entries is now "
                f"{rederived_previous[:12]} but checkpoint {newest.key_id} published "
                f"{newest.merkle_root[:12]}. The prefix of the log changed."
            )
            return _refusal(
                REFUSAL_INCONSISTENT_HISTORY,
                tree_size=tree_size,
                previous_tree_size=newest.tree_size,
                previous_root=newest.merkle_root,
                rederived_root=rederived_previous,
            )

    checkpoint = await build_checkpoint(
        log,
        tree_size,
        timestamp=timestamp,
        format=FORMAT_C2SP_V2,
        origin=origin,
        previous_digest=bytes.fromhex(previous_digest) if previous_digest else None,
    )
    if checkpoint.merkle_root.hex() != derived_hex:
        # build_checkpoint re-reads the log; if a concurrent append landed
        # between the two reads the root could differ. It cannot be right, so
        # do not sign it.
        logger.error("REFUSING: build_checkpoint derived a different root than the pre-check")
        return _refusal(
            REFUSAL_INCONSISTENT_HISTORY,
            tree_size=tree_size,
            derived_root=derived_hex,
            build_root=checkpoint.merkle_root.hex(),
        )

    signed = sign_checkpoint(checkpoint, signer)
    await save_checkpoint(session, signed)
    logger.info(
        f"Signed checkpoint {checkpoint.describe()} with {signed.key_id} "
        f"(previous {previous_digest[:12] if previous_digest else 'none'})"
    )
    return SigningResult(
        signed=signed,
        status="signed",
        tree_size=tree_size,
        merkle_root=derived_hex,
        previous_digest=previous_digest,
        detail={"key_id": signed.key_id, "algorithm": signed.algorithm, "origin": origin},
    )


async def checkpoint_age_hours(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> float | None:
    """Hours since the newest published checkpoint, or None if there is none.

    This is the honest dead-man's-switch input. It measures the artifact, not
    the cron: if the newest checkpoint is old, the log has not been checkpointed
    lately whether or not a scheduler thinks it fired.
    """
    newest = await _newest_checkpoint(session)
    if newest is None:
        return None
    moment = newest.timestamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (reference - moment).total_seconds() / 3600.0


def watchdog_verdict(
    age_hours: float | None,
    *,
    max_interval_hours: float,
) -> tuple[bool, str]:
    """(healthy, verdict string) for a checkpoint age.

    healthy is True when the newest checkpoint is younger than the interval. A
    log with no checkpoint at all is not healthy: silence is the failure this
    exists to catch.
    """
    if age_hours is None:
        return False, "no checkpoint has been published yet"
    if age_hours > max_interval_hours:
        return False, (
            f"newest checkpoint is {age_hours:.1f}h old, past the "
            f"{max_interval_hours:.1f}h interval"
        )
    return True, f"newest checkpoint is {age_hours:.1f}h old"


async def record_alert(
    session: AsyncSession,
    kind: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one alert row. Best-effort: an alert that cannot be written is logged.

    Called from the refusal path, which already has a result to report; losing
    the alert row to a database problem must not turn a clean refusal into a
    stack trace.
    """
    from src.transparency.alerts import TransparencyAlert  # local import: optional table

    try:
        session.add(TransparencyAlert(kind=kind, detail=detail or {}))
        await session.flush()
    except Exception as exc:  # table missing (migration not applied), etc.
        logger.warning(f"Could not record transparency alert {kind!r}: {type(exc).__name__}: {exc}")


__all__ = [
    "ADVISORY_LOCK_KEY",
    "REFUSAL_ALREADY_SIGNED",
    "REFUSAL_EMPTY_LOG",
    "REFUSAL_EQUIVOCATION",
    "REFUSAL_INCONSISTENT_HISTORY",
    "REFUSAL_LOCK_HELD",
    "REFUSAL_LOG_SMALLER",
    "SigningResult",
    "checkpoint_age_hours",
    "record_alert",
    "sign_next_checkpoint",
    "watchdog_verdict",
]
