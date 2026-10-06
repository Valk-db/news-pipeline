"""Append-only hash-chained observation log -- the raw half of the archive.

Only observations belong here: the bytes a source served, when we fetched them,
and which source. Derived stories, clusters, and scores never enter this log.
They are versioned separately and must stay reproducible from these rows, because
clustering and LLM output change when the code or the model changes, not when
the world changes. Chaining the two together turns a code change into an
apparent change in the world.

Hash scheme n1 (the version prefix is stored on every row so a future scheme
change cannot be silently confused with this one):

    canonical  = json.dumps(payload, sort_keys=True,
                             separators=(",", ":"),
                             ensure_ascii=False,
                             allow_nan=False).encode("utf-8")
    leaf_hash  = sha256(canonical)
    chain_hash = sha256(prev_chain_hash_bytes || leaf_hash_bytes)
    entry 0    chains from GENESIS_CHAIN_HASH

The canonical form is RFC 8785 (JCS) compatible for the subset we store:
UTF-8, sorted keys, no insignificant whitespace, no NaN/Infinity. Floats and
non-string keys are therefore out of contract; payloads carry strings, ints,
bools, and lists.

Two properties worth being explicit about:

1. The index and the timestamp sit outside the leaf hash. An entry's leaf hash
   covers the payload only, so a rewritten timestamp would not break the chain.
   That is deliberate -- the timestamp is fetch metadata that a recovery or a
   backfill may legitimately restate -- but it means callers who need
   timestamp tamper-evidence must put the timestamp inside the payload. This
   module verifies (payload -> leaf_hash) and (leaf_hash, prev -> chain_hash);
   it does not vouch for the stored timestamp.
2. A chain is only tamper-evident once somebody outside the operator holds a
   copy of the head. checkpoint.py signs the head and anchoring.py ships it, so
   this module deliberately stops at the hash.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base

logger = logging.getLogger(__name__)

# Stored with every row so a v2 hash scheme is distinguishable from n1.
HASH_SCHEME = "n1:sha256"

# Entry 0 chains from this value instead of from a parent. Derived once from a
# fixed tag so it is identical in every process, every deployment, and in the
# future Rust port.
GENESIS_TAG = b"news-pipeline/merkle-log/v1"
GENESIS_CHAIN_HASH: bytes = hashlib.sha256(GENESIS_TAG).digest()

# The transparency log owns its own metadata. It is deliberately NOT registered on
# src.schema.models.Base: Base.metadata no longer creates anything, so a model class is
# no longer a schema change, and keeping the log's tables out of Base keeps them out of
# scripts/check_schema.py's drift comparison too.
TransparencyBase = declarative_base()


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Serialize a payload to the canonical bytes that get hashed.

    Sorted keys, no insignificant whitespace, UTF-8, NaN/Infinity rejected.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def leaf_hash(payload: Mapping[str, Any]) -> bytes:
    """sha256 of the canonical payload bytes."""
    return hashlib.sha256(canonical_json(payload)).digest()


def compute_chain_hash(previous_chain_hash: bytes, leaf: bytes) -> bytes:
    """Chain the previous chain hash with a leaf hash. Both are raw digests."""
    return hashlib.sha256(previous_chain_hash + leaf).digest()


def _utc(value: datetime) -> datetime:
    """Normalize to tz-aware UTC. Naive input is read as UTC, which is what
    SQLite hands back for a timezone=True column."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class LogEntry:
    """One append-only observation.

    Frozen because an entry that can be edited after the fact is the whole bug
    this package exists to prevent. `payload` is a dict and therefore still
    mutable from inside -- treat it as read-only, or replace the entry.
    """
    index: int
    timestamp: datetime
    payload: dict[str, Any]
    canonical_payload: bytes  # the exact bytes that were hashed
    leaf_hash: bytes
    chain_hash: bytes

    @property
    def leaf_hash_hex(self) -> str:
        return self.leaf_hash.hex()

    @property
    def chain_hash_hex(self) -> str:
        return self.chain_hash.hex()

    def describe(self) -> str:
        """One-line summary for logs and CLI output."""
        return f"#{self.index} {self.timestamp.isoformat()} leaf={self.leaf_hash_hex[:12]} chain={self.chain_hash_hex[:12]}"


def build_entry(
    index: int,
    payload: Mapping[str, Any],
    *,
    timestamp: datetime | None = None,
    previous_chain_hash: bytes = GENESIS_CHAIN_HASH,
) -> LogEntry:
    """Hash a payload into the entry that goes at `index`."""
    if index < 0:
        raise ValueError(f"index must be >= 0, got {index}")
    canonical = canonical_json(payload)
    leaf = hashlib.sha256(canonical).digest()
    chain = compute_chain_hash(previous_chain_hash, leaf)
    return LogEntry(
        index=index,
        timestamp=_utc(timestamp or datetime.now(UTC)),
        payload=dict(payload),
        canonical_payload=canonical,
        leaf_hash=leaf,
        chain_hash=chain,
    )


def verify_chain(entries: Sequence[LogEntry]) -> bool:
    """True only if every entry hashes as recorded and chains from genesis.

    Checks, in order: contiguous indices from 0, leaf_hash recomputed from the
    payload, the stored canonical bytes still matching the payload, and each
    chain_hash chaining from its predecessor. A rewritten payload, a rewritten
    timestamp payload field, a deleted entry, or a re-ordered log all fail.
    """
    previous = GENESIS_CHAIN_HASH
    for position, entry in enumerate(entries):
        if entry.index != position:
            return False
        expected_canonical = canonical_json(entry.payload)
        if expected_canonical != entry.canonical_payload:
            return False
        if hashlib.sha256(entry.canonical_payload).digest() != entry.leaf_hash:
            return False
        if compute_chain_hash(previous, entry.leaf_hash) != entry.chain_hash:
            return False
        previous = entry.chain_hash
    return True


@runtime_checkable
class MerkleLog(Protocol):
    """The only surface checkpoint.py and proofs.py depend on.

    Two implementations: SqlAlchemyMerkleLog for production persistence and
    InMemoryMerkleLog for tests and the demo.
    """

    async def append(self, payload: Mapping[str, Any]) -> LogEntry:
        """Append one observation and return it. Append only: never updates."""
        ...

    async def get(self, index: int) -> LogEntry | None:
        """Return the entry at `index`, or None if the log is shorter."""
        ...

    async def entries(self) -> tuple[LogEntry, ...]:
        """Every entry in index order."""
        ...

    async def leaf_hashes(self) -> list[bytes]:
        """Leaf hashes in index order."""
        ...

    async def head(self) -> bytes:
        """Latest chain hash, or GENESIS_CHAIN_HASH for an empty log."""
        ...

    async def size(self) -> int:
        """Number of entries."""
        ...


class InMemoryMerkleLog:
    """Pure in-memory log: no database, no I/O, deterministic."""

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []

    async def append(self, payload: Mapping[str, Any]) -> LogEntry:
        entry = build_entry(
            len(self._entries),
            payload,
            previous_chain_hash=await self.head(),
        )
        self._entries.append(entry)
        return entry

    async def get(self, index: int) -> LogEntry | None:
        if index < 0 or index >= len(self._entries):
            return None
        return self._entries[index]

    async def entries(self) -> tuple[LogEntry, ...]:
        return tuple(self._entries)

    async def leaf_hashes(self) -> list[bytes]:
        return [entry.leaf_hash for entry in self._entries]

    async def head(self) -> bytes:
        if not self._entries:
            return GENESIS_CHAIN_HASH
        return self._entries[-1].chain_hash

    async def size(self) -> int:
        return len(self._entries)


class MerkleLogEntry(TransparencyBase):
    """One persisted observation. Append-only: no update path exists.

    canonical_payload is stored rather than recomputed on read so verification
    does not depend on a JSON round trip through the database preserving float
    precision and key order exactly.
    """

    __tablename__ = "merkle_log_entries"
    __table_args__ = (
        Index("ix_merkle_log_entries_timestamp", "timestamp"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    index = Column(Integer, nullable=False, unique=True)  # Gapless; entry 0 is the first append
    hash_scheme = Column(String(32), nullable=False, default=HASH_SCHEME)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    payload = Column(JSON, nullable=False)  # Observation as recorded, e.g. {"url": ..., "fetched_at": ..., "body_sha256": ...}
    canonical_payload = Column(Text, nullable=False)  # Exact UTF-8 bytes that were hashed
    leaf_hash = Column(String(64), nullable=False)  # SHA256 hex of canonical_payload
    chain_hash = Column(String(64), nullable=False)  # SHA256 hex of prev chain_hash || leaf_hash

    def to_entry(self) -> LogEntry:
        return LogEntry(
            index=self.index,
            timestamp=_utc(self.timestamp),
            payload=dict(self.payload),
            canonical_payload=self.canonical_payload.encode("utf-8"),
            leaf_hash=bytes.fromhex(self.leaf_hash),
            chain_hash=bytes.fromhex(self.chain_hash),
        )


class SqlAlchemyMerkleLog:
    """Append-only log backed by an AsyncSession.

    Single writer assumed. Two concurrent appends can both read the same head
    and pick the same index; the unique constraint on `index` turns that into a
    loud failure instead of a forked chain. Serialize appends at the job level.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, payload: Mapping[str, Any]) -> LogEntry:
        rows = await self._session.execute(select(MerkleLogEntry).order_by(MerkleLogEntry.index.desc()).limit(1))
        previous = rows.scalars().first()
        previous_chain_hash = bytes.fromhex(previous.chain_hash) if previous else GENESIS_CHAIN_HASH
        entry = build_entry(
            previous.index + 1 if previous else 0,
            payload,
            previous_chain_hash=previous_chain_hash,
        )
        self._session.add(
            MerkleLogEntry(
                index=entry.index,
                hash_scheme=HASH_SCHEME,
                timestamp=entry.timestamp,
                payload=entry.payload,
                canonical_payload=entry.canonical_payload.decode("utf-8"),
                leaf_hash=entry.leaf_hash_hex,
                chain_hash=entry.chain_hash_hex,
            )
        )
        await self._session.flush()
        return entry

    async def get(self, index: int) -> LogEntry | None:
        # Selected by index rather than session.get(): the primary key is the row
        # UUID, and the log is always addressed by its position.
        rows = await self._session.execute(select(MerkleLogEntry).where(MerkleLogEntry.index == index))
        row = rows.scalars().first()
        return row.to_entry() if row is not None else None

    async def entries(self) -> tuple[LogEntry, ...]:
        rows = await self._session.execute(select(MerkleLogEntry).order_by(MerkleLogEntry.index))
        return tuple(row.to_entry() for row in rows.scalars().all())

    async def leaf_hashes(self) -> list[bytes]:
        rows = await self._session.execute(
            select(MerkleLogEntry.leaf_hash).order_by(MerkleLogEntry.index)
        )
        return [bytes.fromhex(value) for value in rows.scalars().all()]

    async def head(self) -> bytes:
        rows = await self._session.execute(
            select(MerkleLogEntry.chain_hash).order_by(MerkleLogEntry.index.desc()).limit(1)
        )
        value = rows.scalars().first()
        return bytes.fromhex(value) if value else GENESIS_CHAIN_HASH

    async def size(self) -> int:
        rows = await self._session.execute(select(MerkleLogEntry.index).order_by(MerkleLogEntry.index.desc()).limit(1))
        last = rows.scalars().first()
        return last + 1 if last is not None else 0