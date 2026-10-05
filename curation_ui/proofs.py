"""Public inclusion-proof permalinks: the server-rendered view model.

The route lives in curation_ui.public_pages (/proof/{article_id}); this module builds
the view. Read-only: nothing here writes to the session.

States, all rendered honestly:
- verified: entry found, bound to this article, a checkpoint covers it, the
  math checks out, AND the checkpoint's signature verifies against a
  published operator key (TRANSPARENCY_TRUSTED_KEYS). This is the only state
  that may claim the log is "signed".
- unverified_signature: the math and the article binding check out, but the
  checkpoint's signature does not verify against any published key (unknown
  key id, bad signature, development HMAC key, no published keys configured,
  or the cryptography package missing). The inclusion math is shown because
  it is independently recomputable; the "signed" claim is not made.
- failed: entry and checkpoint exist but the math or the article binding does
  not check out (tampered payload, wrong siblings, checkpoint mismatch,
  repointed log_index). Shown, not hidden.
- pending_unstamped: article.log_index is NULL, never stamped. The page says
  which of the two reasons applies (no stampable body archived, or archived by
  the feed ingest that does not stamp) and reports archive-wide stamping scale,
  so a reader can tell "normal for most of this archive" apart from "the
  stamping pass is not running".
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
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from src.schema.models import RawArticle
from src.shared.config import get_settings
from src.transparency.checkpoint import SignedCheckpoint, verify_checkpoint
from src.transparency.keys import TRUSTED_KEYS_ENV_VAR, load_trusted_keys, verifier_for
from src.transparency.log import MerkleLogEntry, SqlAlchemyMerkleLog, canonical_json, compute_chain_hash
from src.transparency.proofs import inclusion_proof, proof_root, proof_steps, verify_inclusion
from src.transparency.store import checkpoints_covering

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


# The body length below which src/ingestion/rss_evidence.py refuses to treat a
# page as an article (its MIN_BODY_CHARS), so a row with a shorter body could
# not have been stamped even if the locker had seen it.
#
# Copied rather than imported on purpose: rss_evidence imports spacy, trafilatura
# and the whole ingestion stack, and this module is imported by the Vercel
# function bundle. tests/test_proof_permalinks.py pins the two values together
# so a change to one without the other fails the suite.
MIN_STAMPABLE_BODY_CHARS = 200

# How old the newest log entry may get before the page says the stamping pass
# looks stalled. The workflow runs daily at 04:47 UTC, so 48h is two whole
# missed runs before this is worth telling a reader about.
STAMP_STALE_HOURS = 48


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


def _as_utc(value: datetime | None) -> datetime | None:
    """Naive timestamps are read as UTC.

    Same reason as src/transparency/log.py::_utc: SQLite returns a naive
    datetime for a DateTime(timezone=True) column, and subtracting that from an
    aware now() is a TypeError, which would 500 a public page over a cosmetic
    age calculation.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _stamping_scale(session) -> dict[str, Any] | None:
    """Archive-wide stamping counters, or None if they cannot be read.

    Three cheap aggregate/index-only queries. Used to tell a reader whether
    "no proof for this article" is the normal state of the archive or the
    symptom of a stamping pass that has stopped. Never raises: a public page
    must still render its (truthful) pending state when a counter is
    unavailable.
    """
    try:
        stamped = (
            await session.execute(
                select(func.count()).select_from(RawArticle).where(RawArticle.log_index.is_not(None))
            )
        ).scalar_one()
        total = (await session.execute(select(func.count()).select_from(RawArticle))).scalar_one()
        newest = (
            await session.execute(select(func.max(MerkleLogEntry.timestamp)))
        ).scalar_one()
    except Exception as exc:  # noqa: BLE001 - a public page degrades, never 500s
        logger.warning("Proof page could not read stamping scale: %s", exc)
        return None
    return {
        "stamped": int(stamped),
        "total": int(total),
        "newest_entry_at": _as_utc(newest),
    }


def _scale_sentence(scale: dict[str, Any]) -> str:
    """One plain sentence of archive-wide scale, from the counters."""
    stamped, total = scale["stamped"], scale["total"]
    if not total:
        return "The archive holds no articles yet."
    pct = 100.0 * stamped / total
    parts = [f"{stamped} of {total} archived articles carry an inclusion stamp ({pct:.2f}%)."]
    newest = scale.get("newest_entry_at")
    if newest is None:
        parts.append("The log itself is empty, so nothing has ever been stamped.")
        return " ".join(parts)
    age_hours = (datetime.now(UTC) - newest).total_seconds() / 3600
    if age_hours < 0:
        parts.append(f"The newest stamp is timestamped {newest.isoformat()}.")
    else:
        parts.append(f"The newest stamp is {age_hours:.1f} hours old ({newest.isoformat()}).")
        if age_hours > STAMP_STALE_HOURS:
            parts.append(
                "That is older than the daily stamping run should allow, so the "
                "stamping pass looks stalled rather than this article being "
                "an exception."
            )
    return " ".join(parts)


async def _unstamped_view(session, article: RawArticle) -> ProofView:
    """pending_unstamped, with the reason spelled out instead of guessed at.

    The page used to say only "this article has not been stamped yet", which
    reads identically whether the stamping workflow is broken or the row was
    archived by a path that never stamps. Both facts are observable, so both
    are stated: whether a stampable body was archived at all, and how much of
    the archive carries a stamp.
    """
    body_len = len(article.body_text or "")
    has_body = body_len >= MIN_STAMPABLE_BODY_CHARS
    if has_body:
        detail = (
            f"This article is archived with a {body_len}-character body, so there "
            f"was something to hash into a log entry, but it was archived by the "
            f"ordinary feed ingest, which does not stamp. Stamping is applied by "
            f"the evidence locker to the articles it archives itself, and it is "
            f"not applied retroactively to rows already in the archive. So this is "
            f"a gap in coverage, not a broken proof: there is no log entry for "
            f"this article and none is invented here."
        )
    else:
        detail = (
            f"This article is archived with only {body_len} characters of text, "
            f"below the {MIN_STAMPABLE_BODY_CHARS}-character minimum the evidence "
            f"locker requires before it treats a page as an article. A headline "
            f"with no body is not stamped, so no log entry exists for it."
        )

    view = _pending_view(
        article,
        "pending_unstamped",
        "Proof pending: this article has not been stamped into the log"
        + ("" if has_body else " (no stampable body)"),
        detail,
    )
    scale = await _stamping_scale(session)
    if scale is not None:
        sentence = _scale_sentence(scale)
        view.state_detail = f"{view.state_detail} {sentence}"
        view.checks.append(ProofCheck(
            label="Archive-wide stamping scale",
            passed=None,
            detail=sentence,
        ))
    return view


async def _chain_reaches_head(session, entry, checkpoint) -> bool:
    """True iff the log's chain from `entry` reaches the checkpoint's head.

    Folds entry.chain_hash forward through the stored leaf hashes of every
    later covered entry and compares against checkpoint.chain_hash. This binds
    the checkpoint to the log rows between the entry and the signed head: a
    checkpoint computed from a different history fails even when its Merkle
    root is self-consistent, because the chain hash commits to the whole
    sequence, not just the set of leaves.
    """
    if checkpoint.tree_size <= entry.index:
        return False
    rows = await session.execute(
        select(MerkleLogEntry.leaf_hash)
        .where(
            MerkleLogEntry.index > entry.index,
            MerkleLogEntry.index < checkpoint.tree_size,
        )
        .order_by(MerkleLogEntry.index)
    )
    chain = entry.chain_hash
    seen = 0
    for (leaf_hex,) in rows.all():
        try:
            leaf = bytes.fromhex(leaf_hex)
        except ValueError:
            return False
        chain = compute_chain_hash(chain, leaf)
        seen += 1
    if seen != checkpoint.tree_size - entry.index - 1:
        # Gap in the covered range: the log cannot substantiate this head.
        return False
    return chain == checkpoint.chain_hash


def _signature_verdict(signed: SignedCheckpoint, trusted: dict) -> tuple[bool, str]:
    """Check the checkpoint's signature against the published operator keys.

    Returns (verified, detail). A False here never means "the math failed":
    it means the "signed" claim is unproven, and the page must say exactly
    that instead of rendering a verified badge.
    """
    verifier = verifier_for(signed, trusted)
    if verifier is None:
        if not trusted:
            reason = (
                f"the operator has not published any verification keys "
                f"({TRUSTED_KEYS_ENV_VAR} is empty)"
            )
        elif signed.key_id not in trusted:
            reason = f"key {signed.key_id} is not among the operator's published keys"
        else:
            reason = (
                f"key {signed.key_id} is published but its signature cannot be "
                f"checked here (see the server log)"
            )
        return False, (
            f"{signed.algorithm}, key {signed.key_id}: signature NOT verified, {reason}. "
            f"The inclusion math below is independently recomputable, but do not "
            f"treat this checkpoint as operator-signed."
        )
    if verify_checkpoint(signed, verifier):
        return True, (
            f"{signed.algorithm}, key {signed.key_id}: signature verifies against "
            f"the operator's published public key."
        )
    return False, (
        f"{signed.algorithm}, key {signed.key_id}: the signature does not check "
        f"out against the published public key. The checkpoint may be forged or "
        f"corrupted; do not treat it as operator-signed."
    )


def _binding_verdict(entry_payload: dict[str, Any], article: RawArticle) -> tuple[bool | None, str]:
    """Check that the log entry was stamped for THIS article.

    Returns (bound, detail) where bound is True (payload names this article),
    False (payload names a different article, or the archived fields disagree
    with the article row: the log_index was repointed or the row edited), or
    None (legacy entry stamped before article_id binding existed: the
    URL/title/body match below is the binding, and the page discloses that).
    """
    payload_article_id = entry_payload.get("article_id")
    article_id = str(article.id)
    fields = (
        ("url", article.url),
        ("title", article.title),
        ("body_sha256", article.content_hash),
    )
    mismatched = [name for name, value in fields if entry_payload.get(name) != value]
    if payload_article_id is not None:
        if payload_article_id != article_id:
            return False, (
                f"the entry was stamped for article {payload_article_id}, not this "
                f"article ({article_id}). The log_index link does not point at this "
                f"article's own evidence."
            )
        if mismatched:
            return False, (
                f"the entry is stamped for this article but the archived "
                f"{', '.join(mismatched)} no longer match the article row: the row "
                f"was edited after stamping."
            )
        return True, (
            f"the entry's payload names this article ({article_id}) and the "
            f"archived url/title/body_sha256 match the article row."
        )
    # Legacy entry: stamped before article_id was bound into the leaf.
    if mismatched:
        return False, (
            f"legacy entry (stamped before article binding existed): the archived "
            f"{', '.join(mismatched)} do not match the article row."
        )
    return None, (
        "legacy entry, stamped before article_id was bound into the leaf: the "
        "binding rests on the archived url/title/body_sha256 matching the "
        "article row, which they do. New stamps bind the article id "
        "cryptographically."
    )


async def _pick_verified_checkpoint(session, index: int, trusted: dict):
    """Newest checkpoint covering `index` whose signature verifies, if any.

    Walks candidates newest-first so a forged row (newer, self-consistent,
    signed by no known key) cannot displace the genuine checkpoint: the page
    anchors to the newest *verifiable* checkpoint. Returns (signed, verified,
    skipped_unverified) where skipped_unverified counts candidates whose
    signatures did not verify.
    """
    candidates = await checkpoints_covering(session, index)
    skipped = 0
    for candidate in candidates:
        verifier = verifier_for(candidate, trusted)
        if verifier is not None and verify_checkpoint(candidate, verifier):
            return candidate, True, skipped
        skipped += 1
    newest = candidates[0] if candidates else None
    return newest, False, skipped


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
        return await _unstamped_view(session, article)

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

    trusted = load_trusted_keys(get_settings().transparency_trusted_keys)
    try:
        signed, _sig_ok, skipped_unverified = await _pick_verified_checkpoint(
            session, entry.index, trusted
        )
    except Exception as exc:
        if _is_missing_table(exc):
            signed, _sig_ok, skipped_unverified = None, False, 0
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

    # The scheme the operator actually signed with, never the legacy default.
    # sign_next_checkpoint writes c2sp-tlog-checkpoint-v3, whose root is an
    # RFC 6962 tree (domain-separated leaves, no odd-level duplication), so a
    # path or fold built with the default n1-ct-dup-v1 scheme lands on a
    # different root than the checkpoint signs and a genuine proof renders as
    # "failed". verify_inclusion below already reads the scheme off the
    # checkpoint; these three call sites have to agree with it.
    scheme = signed.checkpoint.tree_scheme()
    proof = await inclusion_proof(
        log, entry.index, signed.checkpoint.tree_size, scheme=scheme
    )
    math_ok = verify_inclusion(entry, proof, signed)
    bound, binding_detail = _binding_verdict(entry.payload, article)
    chain_ok = await _chain_reaches_head(session, entry, signed.checkpoint)
    sig_ok, sig_detail = _signature_verdict(signed, trusted)

    # Explanatory breakdown of the halves verify_inclusion checks.
    try:
        leaf_recomputed = hashlib.sha256(canonical_json(entry.payload)).digest() == entry.leaf_hash
    except TypeError:
        leaf_recomputed = False
    root_recomputed = (
        proof_root(entry.leaf_hash, proof.index, proof.siblings, scheme)
        == signed.checkpoint.merkle_root
    )

    intact = math_ok and chain_ok and bound is not False
    if intact and sig_ok:
        state = "verified"
        state_headline = "Inclusion verified: this article is in the signed log"
        state_detail = (
            f"Log entry #{entry.index} folds through {len(proof.siblings)} sibling "
            f"hashes to the checkpoint root, the checkpoint's chain reaches this "
            f"entry, and the checkpoint signature verifies against the operator's "
            f"published key {signed.key_id}."
        )
    elif intact:
        state = "unverified_signature"
        state_headline = "Inclusion math verified, checkpoint signature unverified"
        state_detail = (
            f"Log entry #{entry.index} folds to the checkpoint root and is bound "
            f"to this article, but the checkpoint's signature does not verify "
            f"against any published operator key. The math below is independently "
            f"recomputable; the 'signed' claim is unproven"
            + (f" ({skipped_unverified} checkpoint(s) checked, none verified)."
               if skipped_unverified else ".")
        )
    else:
        state = "failed"
        state_headline = "Verification failed: this proof does not check out"
        state_detail = (
            "The entry's payload does not match its leaf hash, the sibling fold "
            "does not reach the checkpoint root, the checkpoint's chain does not "
            "reach this entry, or the entry is not bound to this article. Treat "
            "this article's archive claim as unproven until the mismatch is "
            "explained."
        )

    steps = [
        {
            "level": step.level,
            "position": step.position,
            "left": step.left.hex(),
            "right": step.right.hex(),
            "digest": step.digest.hex(),
            "note": _describe_step(step.level, step.position),
        }
        for step in proof_steps(entry.leaf_hash, proof.index, proof.siblings, scheme)
    ]

    checkpoint = signed.checkpoint
    binding_label = (
        "Log entry is bound to this article"
        if bound is not None else
        "Log entry binding (legacy stamp)"
    )
    return ProofView(
        **identity,
        state=state,
        state_headline=state_headline,
        state_detail=state_detail,
        checks=[
            ProofCheck(
                label="Article stamped into the transparency log",
                passed=True,
                detail=f"log entry #{entry.index}, leaf {entry.leaf_hash_hex[:16]}...",
            ),
            ProofCheck(
                label=binding_label,
                passed=bound,
                detail=binding_detail,
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
                label="Checkpoint chain reaches this entry",
                passed=chain_ok,
                detail=(
                    f"the chain from entry #{entry.index} reaches the checkpoint "
                    f"head (chain {checkpoint.chain_hash.hex()[:16]}...)"
                    if chain_ok else
                    "the checkpoint's chain hash does not follow from this entry: "
                    "the checkpoint was computed from a different history"
                ),
            ),
            ProofCheck(
                label="Checkpoint signature verifies against a published key",
                passed=sig_ok,
                detail=sig_detail,
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
            "signature_verified": sig_ok,
            "signing_bytes": signed.signing_bytes().decode("ascii", errors="replace"),
        },
    )
