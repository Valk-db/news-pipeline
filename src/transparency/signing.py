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

WHY THE CHECKS BELOW ARE NOT REDUNDANT WITH EACH OTHER
=======================================================

Every check in this module used to compare the log against the checkpoint table
and treat a disagreement as a contradiction. That is sound only if the table is
an honest witness, and it is not: an attacker who can write to the database can
rewrite the leaf rows AND the checkpoint rows to agree with each other, and then
every comparison here passes. The table is the thing under attack, so it cannot
be the thing that establishes trust. The checks that actually close that are:

F1  previous signature. Before the newest checkpoint's row is believed, its
    detached signature is verified. This is the only check in the module that
    does not compare two things the same attacker can edit. Tampering with a
    stored root, chain hash or tree_size breaks the signature even when the row
    is otherwise intact, because the signature covers all of it.
F2  an externally recorded head. An attacker who deletes or truncates the
    checkpoint table leaves the log looking brand new, and every intra-table
    comparison agrees. A head value held OUTSIDE this database does not, so the
    signer requires one and refuses when the table falls behind it.
F5  the log's own shape. len(entries) must equal the reported tree_size and the
    indices must be contiguous from 0, checked before anything is derived.

Read together: F1 makes a forged row unusable, F2 makes a deleted one
detectable, and F5 makes a gap a refusal instead of a 500.

WHAT F2's HEAD DOES AND DOES NOT BUY
====================================

`transparency_signed_head` is a configured value of the form

    "<tree_size>:<checkpoint_digest_hex>"

naming the last checkpoint the operator published, recorded OUTSIDE this
database. It buys exactly one thing: against an attacker who can write to the
database but not to the deploy's configuration, a rollback, truncation, or
deletion of published checkpoints is detected, because the head does not move
with them.

It does NOT buy protection against an attacker who can rewrite the deploy's
environment. Such an attacker edits the head and the log together, and the head
becomes theatre. It is a floor, not a witness. A real witness -- a digest handed
to OpenTimestamps, a co-signing service we do not control, an archived copy --
does not share that weakness, and that is Phase 2 (src/transparency/anchoring.py
is the interface and a deliberate stub for it). Until one exists, this module
provides a smaller true guarantee rather than a larger pretended one, and the
gap is named here so nobody has to discover it.

The head is advanced out of band by the operator after a successful run; the
signing result carries the value to advance to, in
`detail["published_head"]`. It cannot advance itself, and pretending otherwise
would be the theatre this paragraph exists to rule out.

This module is HTTP-free on purpose. curation_ui/cron.py is the transport;
everything here can be called from a script or a test with a session and a
signer, and the cron route can be tested without a database or a key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.transparency.checkpoint import (
    FORMAT_C2SP_V3,
    FORMAT_JSON_V1,
    TREE_SCHEME_CT_V1,
    TREE_SCHEME_FOR_FORMAT,
    TREE_SCHEME_RFC6962,
    SignedCheckpoint,
    Signer,
    build_checkpoint,
    checkpoint_digest,
    root_for_scheme,
    sign_checkpoint,
    verify_checkpoint,
)
from src.transparency.keys import TrustedKey, verifier_for
from src.transparency.keys import _as_utc  # noqa: PLC2701 - same-module time normalisation
from src.transparency.log import LogEntry, MerkleLog
from src.transparency.store import (
    TransparencyCheckpoint,
    TransparencyQuarantine,
    save_checkpoint,
)

logger = logging.getLogger(__name__)

# Rows scanned when looking for the newest non-quarantined checkpoint. Bounded
# so a table carrying many quarantined rows cannot turn one lookup into an
# unbounded scan; a value beyond this is a table that needs a human, not a
# signer that keeps reading.
_QUERY_LIMIT = 1000

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

# F1. The previous checkpoint's row could not be authenticated: no published
# key for the key_id it claims (REFUSAL_PREVIOUS_KEY_UNKNOWN), the published key
# is not valid for that checkpoint's size and time
# (REFUSAL_PREVIOUS_KEY_OUT_OF_BOUNDS), or the signature does not verify against
# it (REFUSAL_PREVIOUS_SIGNATURE_INVALID). The last is the one that catches a
# tampered stored root.
REFUSAL_PREVIOUS_KEY_UNKNOWN = "previous_key_unknown"
REFUSAL_PREVIOUS_KEY_OUT_OF_BOUNDS = "previous_key_out_of_bounds"
REFUSAL_PREVIOUS_SIGNATURE_INVALID = "previous_signature_invalid"

# F2. The database's checkpoint table does not account for a head recorded
# outside it. REFUSAL_HEAD_AHEAD_OF_LOG means the table is missing checkpoints
# that were published (deleted, truncated, or hidden by RLS) -- the case that
# used to be indistinguishable from genesis. REFUSAL_HEAD_MISMATCH means a row
# exists at the head's tree_size but is not the checkpoint that was published
# there. REFUSAL_GENESIS_UNCONFIRMED means there is no checkpoint at all and the
# operator has not explicitly confirmed the genesis.
REFUSAL_HEAD_AHEAD_OF_LOG = "head_ahead_of_log"
REFUSAL_HEAD_MISMATCH = "head_mismatch"
REFUSAL_GENESIS_UNCONFIRMED = "genesis_unconfirmed"

# F3. The signer refuses before signing unless its own key is published in the
# trusted set and in bounds, and refuses after signing unless its own output
# verifies. The second is what catches a silently mis-derived key (a pasted raw
# key or a changed derivation producing a different key than the operator
# published) rather than signing an uncheckable history forever.
REFUSAL_SIGNER_KEY_UNTRUSTED = "signer_key_untrusted"
REFUSAL_SIGNER_KEY_OUT_OF_BOUNDS = "signer_key_out_of_bounds"
REFUSAL_SIGNER_VERIFY_FAILED = "signer_verify_failed"
# The two post-sign checks are deliberately different codes, not one: "this
# signature is corrupt" and "the key that produced it is not the key the
# operator published" are different failures with different operator actions, and
# a single code would also make the two guards indistinguishable to a mutation
# test (removing either one would leave the other's code passing).
REFUSAL_SIGNER_KEY_MISMATCH = "signer_key_mismatch"

# F5. The log's own shape is wrong before any comparison happens: log.size()
# reports max(index)+1, so a deleted middle row makes tree_size exceed the number
# of rows actually present. That used to reach verify_chain and raise, which the
# cron turned into a 500 on every subsequent run.
REFUSAL_LOG_SHAPE = "log_shape_invalid"

# F10. A tamper-induced ValueError (a rewritten payload, a broken chain, an
# entry count that does not match the tree size) is a REFUSAL with an alert, not
# an exception. It used to propagate out of the route as a 500: the operator saw
# "server error", nothing was written to the alert table, and every subsequent
# run failed the same way with no record of why.
REFUSAL_CHAIN_INVALID = "chain_invalid"

# F8. The checkpoint's origin does not match the signing key's name, so the key
# would be signing for a log it was not issued for. sign_checkpoint raises; the
# signer turns that into a refusal and an alert rather than a 500.
REFUSAL_ORIGIN_KEY_MISMATCH = "origin_key_mismatch"

# F15. The new checkpoint's timestamp is earlier than the previous one's, so the
# published chain runs backwards in time. A watchdog that ages checkpoints would
# read that as the newest checkpoint being old, and a reader walking the chain
# would see time go backwards.
REFUSAL_TIMESTAMP_BACKDATED = "timestamp_backdated"

# The tree scheme and signed format the signer PUBLISHES. Everything the signer
# derives, compares, and stores is under this scheme; rows from another scheme
# are a different chain and are never compared against it (F9). An old-scheme row
# at the same tree size has a different root over the same leaves, so comparing
# them would either refuse forever or, worse, pass by coincidence.
SIGNER_FORMAT = FORMAT_C2SP_V3
SIGNER_TREE_SCHEME = TREE_SCHEME_RFC6962

# F10. Repeated refusals of the same kind are deduped in the alert table for
# this many seconds. The lock_held case is the one that matters: a second cron
# fire every minute while the first is stuck writes a row a minute, which buries
# every other alert in the table the operator reads to find out what is wrong.
ALERT_DEDUPE_SECONDS = 3600.0



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


def _row_scheme(row: TransparencyCheckpoint) -> str:
    """Which tree hashing produced a stored row's root (F9).

    The column arrived with the v3 migration and is NOT NULL, so this is a
    straight read. A row written before the migration existed has None, which is
    only reachable on a database the migration has not been applied to; it is
    mapped to the scheme its format implies rather than trusted as a new scheme.
    """
    scheme = getattr(row, "tree_scheme", None)
    if scheme:
        return str(scheme)
    return TREE_SCHEME_FOR_FORMAT.get(_row_format(row), TREE_SCHEME_CT_V1)



async def quarantined_checkpoint_ids(session: AsyncSession) -> set[str]:
    """The ids an operator has quarantined. Empty when the table is absent.

    A quarantined row is not deleted -- it cannot be: the append-only trigger
    from 20261002193000 forbids DELETE and the signer role holds no DELETE at
    all, which is correct (a signer that could remove an inconvenient row is an
    equivocation primitive). So exclusion is recorded in a separate append-only
    table, INSERT-only for the signer, and the signer skips those ids when it
    picks the previous checkpoint.

    The head check (F2) is what keeps this from being a hole: quarantining the
    genuine newest row moves the signer back to an older one, but the head does
    not move, so the run still refuses with head_ahead_of_log. Quarantine can
    never be used to get the signer to sign below what was published.
    """
    try:
        rows = await session.execute(select(TransparencyQuarantine.checkpoint_id))
    except Exception as exc:  # table missing (migration not applied)
        logger.info(f"Could not read transparency_quarantine: {type(exc).__name__}")
        return set()
    return {str(value) for value in rows.scalars().all() if value is not None}


async def _newest_checkpoint(
    session: AsyncSession,
    *,
    skip_ids: frozenset[str] = frozenset(),
    scheme: str | None = None,
) -> TransparencyCheckpoint | None:
    """The published checkpoint covering the most entries, if any.

    Tie-broken by created_at, newest row first. The tie is real now: a v2
    checkpoint is published at the same tree size as the v1 checkpoint it
    replaces, because v1 rows are append-only and cannot be rewritten. Ordering
    by insertion time is what makes the chain walk forward through that pair
    instead of picking between them arbitrarily.

    `skip_ids` excludes quarantined rows (see quarantined_checkpoint_ids).

    `scheme` restricts the search to one tree scheme (F9). A v2/CT root and a
    v3/RFC-6962 root over the same leaves are different numbers, so a row from a
    superseded scheme is not a predecessor: it belongs to a different chain, and
    treating it as one would make the prefix re-derivation refuse forever
    against a root that was never going to match. The signer passes the scheme it
    publishes, so the chain it walks is the chain it extends.
    """
    query = select(TransparencyCheckpoint).order_by(
        TransparencyCheckpoint.tree_size.desc(),
        TransparencyCheckpoint.created_at.desc(),
    )
    if scheme is not None:
        query = query.where(TransparencyCheckpoint.tree_scheme == scheme)
    rows = await session.execute(query.limit(_QUERY_LIMIT))
    for row in rows.scalars().all():
        if str(row.id) not in skip_ids:
            return row
    return None


async def superseded_checkpoint_count(
    session: AsyncSession, *, scheme: str = SIGNER_TREE_SCHEME
) -> int:
    """How many published checkpoints belong to a superseded tree scheme (F9).

    Reported rather than hidden: after the scheme change the old chain is not
    deleted (the tables are append-only and it should not be), so an operator
    looking at the checkpoint table needs to be told that those rows are a
    different chain, not a gap or an equivocation.
    """
    result = await session.execute(
        select(func.count())
        .select_from(TransparencyCheckpoint)
        .where(TransparencyCheckpoint.tree_scheme != scheme)
    )
    return int(result.scalar() or 0)



@dataclass(frozen=True)
class TrustedHead:
    """The last checkpoint published, recorded OUTSIDE this database.

    Parsed from `transparency_signed_head`, which is
    "<tree_size>:<checkpoint_digest_hex>" -- the digest being checkpoint_digest()
    of that checkpoint, the same value chained into the next one as
    previous_digest. It is compared against what the database claims, never
    merged into it, which is the entire point: it is the one input to the signer
    an attacker with database write access cannot move.

    tree_size is the published checkpoint's own size, not a minimum. A head that
    the database has never heard of is REFUSAL_HEAD_AHEAD_OF_LOG; a database that
    has moved past it is fine, because the head is only a floor.
    """

    tree_size: int
    digest: str


def parse_trusted_head(raw: str | None) -> TrustedHead | None:
    """Parse the configured head. None for absent or unusable input.

    Unparseable is treated as absent rather than as a default, and the caller
    decides what absent means: with a genesis confirmation it means "nothing has
    been published yet", and without one the signer refuses. So a typo in the
    setting can never silently downgrade the guarantee to "trust the table".
    """
    value = (raw or "").strip()
    if not value:
        return None
    tree_size, _, digest = value.partition(":")
    digest = digest.strip().lower()
    try:
        size = int(tree_size)
    except ValueError:
        logger.error(
            "transparency_signed_head %r does not start with an integer tree_size; "
            "treating it as unset, which means genesis must be confirmed explicitly",
            value[:40],
        )
        return None
    if size < 1 or len(digest) != 64:
        logger.error(
            "transparency_signed_head %r is not <tree_size>:<64-hex-digest>; "
            "treating it as unset, which means genesis must be confirmed explicitly",
            value[:40],
        )
        return None
    try:
        bytes.fromhex(digest)
    except ValueError:
        logger.error(
            "transparency_signed_head digest is not hex; treating it as unset, "
            "which means genesis must be confirmed explicitly"
        )
        return None
    return TrustedHead(tree_size=size, digest=digest)


def _published_head_value(tree_size: int, digest: str) -> str:
    """The string an operator advances transparency_signed_head to."""
    return f"{tree_size}:{digest}"


async def _checkpoints_at_size(
    session: AsyncSession,
    tree_size: int,
    *,
    skip_ids: frozenset[str] = frozenset(),
    scheme: str | None = None,
) -> list[TransparencyCheckpoint]:
    """Every non-quarantined published checkpoint at exactly this tree size.

    All of them, not just the first: the migration adds UNIQUE(tree_size), but a
    database predating it can hold several rows at one size, and two rows with
    different roots at the same size is precisely the equivocation this function
    exists to catch. Reading only the first would let the second hide.

    `skip_ids` excludes quarantined rows, and that is load-bearing rather than
    cosmetic. It was MISSING here when the live probe ran, and the symptom was
    subtle: a quarantined row still matched the idempotency check, so a run whose
    whole purpose was to sign past a quarantined row returned already_signed
    forever and the signer never recovered. Quarantine that the signer still
    reads is not quarantine. Found by running the real path against dev, not by a
    unit test: every SQLite test seeds a table with nothing to quarantine.
    """
    query = select(TransparencyCheckpoint).where(TransparencyCheckpoint.tree_size == tree_size)
    if scheme is not None:
        # F9: a root is only comparable within one tree scheme.
        query = query.where(TransparencyCheckpoint.tree_scheme == scheme)
    rows = await session.execute(query)
    return [row for row in rows.scalars().all() if str(row.id) not in skip_ids]


async def sign_next_checkpoint(
    session: AsyncSession,
    log: MerkleLog,
    signer: Signer,
    *,
    origin: str,
    timestamp: datetime | None = None,
    lock: bool = True,
    trusted_keys: Mapping[str, TrustedKey] | None = None,
    head: TrustedHead | None = None,
    genesis_confirmed: bool = False,
) -> SigningResult:
    """Sign a checkpoint over the whole log, or refuse with a reason.

    `origin` is the log identity written into a v2 note and required by
    tlog-checkpoint; it is a parameter rather than a constant so a test can use
    its own and so a future second log is a call site change, not a code edit.

    The three trust inputs are explicit keyword arguments and all default to the
    REFUSING state, never to a permissive one:

    trusted_keys   Mapping[str, TrustedKey] from keys.load_trusted_keys. Used to
                   verify the previous checkpoint (F1) and to confirm this
                   signer's own key is published and in bounds (F3). Empty means
                   "no published keys", and signing refuses.
    head           TrustedHead recorded outside this database (F2). None means
                   "nothing has been published", which is only acceptable for
                   the genesis checkpoint and only when genesis_confirmed is set.
    genesis_confirmed
                   The operator has explicitly asserted that this log's first
                   checkpoint is legitimate. Without it a log with no checkpoints
                   at all refuses, because RLS hiding every row, a truncated
                   table and a genuine first run are otherwise identical.
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

    # F5, before anything is derived from the entries. log.size() is
    # max(index)+1, so a deleted or planted index gap makes tree_size exceed the
    # number of rows actually present. The old code sliced entries[:tree_size],
    # silently derived a root over fewer leaves than the number it was about to
    # claim, and let build_checkpoint's verify_chain raise -- which the cron
    # surfaced as a 500 on every run thereafter, with nothing written to the
    # alert table. Contiguity is also what makes the root comparable to a
    # published one: a root over a set with a hole is not a root over this log.
    entries = await log.entries()
    shape_problem = _log_shape_problem(entries, tree_size)
    if shape_problem is not None:
        logger.error(
            f"REFUSING: the log claims {tree_size} entries but its shape is {shape_problem}. "
            "A gap or a duplicate index means the root cannot be compared to any "
            "published checkpoint."
        )
        return _refusal(REFUSAL_LOG_SHAPE, tree_size=tree_size, detail_problem=shape_problem,
                        entry_count=len(entries))

    skip_ids = frozenset(await quarantined_checkpoint_ids(session))
    # TWO row selections, and the difference is the whole fix. F9 introduced the
    # scheme column and initially scoped every lookup to the scheme this signer
    # publishes. That was wrong, and wrong in the direction that matters: a
    # scheme filter does not merely stop the signer comparing two chains, it
    # makes a row in another scheme INVISIBLE to the security checks. An attacker
    # holding database write access -- the exact adversary F1 and F2 exist for --
    # defeats both by writing the forged or planted row under the legacy v2
    # format instead of v3. Measured: with the scheme filter on, all nine of the
    # F1/F2/F5/quarantine tests in tests/test_transparency_signer_hardening.py
    # signed over a forgery instead of refusing.
    #
    # The rule, and it is short: a published row is invisible to the signer's
    # security checks ONLY if an operator quarantined it. Otherwise every row
    # participates, each re-derived under its own scheme. Scheme scoping belongs
    # to CHAIN CONTINUITY alone -- a CT root is genuinely not the predecessor of
    # an RFC-6962 root -- and never to whether a row is authentic (F1), whether
    # the head is still the floor (F2), whether the log went backwards (F15), or
    # whether the size is equivocated.
    newest = await _newest_checkpoint(session, skip_ids=skip_ids)
    # Chain continuity: the tip of the chain this signer actually extends.
    newest_in_scheme = await _newest_checkpoint(session, skip_ids=skip_ids, scheme=SIGNER_TREE_SCHEME)
    previous: SignedCheckpoint | None = newest.to_signed() if newest is not None else None
    previous_digest = (
        checkpoint_digest(newest_in_scheme.to_signed()) if newest_in_scheme is not None else None
    )
    # F9, reported not hidden: rows from the superseded CT scheme stay in the
    # table (it is append-only) but are not part of this chain.
    superseded = await superseded_checkpoint_count(session)
    if superseded:
        logger.warning(
            f"{superseded} published checkpoint(s) use a superseded tree scheme and are "
            f"not part of the {SIGNER_TREE_SCHEME} chain this signer extends"
        )

    # F2 first, before the previous row is believed for anything at all. The
    # head is the only input here the database cannot move, so the head is what
    # decides whether the absence of a row means "never signed" or "rows were
    # removed". Ordering matters: F1 below can only authenticate a row that
    # exists, and a deleted row is exactly the case F1 cannot see.
    head_verdict = _check_against_head(newest, previous_digest, head, genesis_confirmed=genesis_confirmed)
    if isinstance(head_verdict, SigningResult):
        return head_verdict

    # F1, immediately after and before the previous row is compared to anything.
    # Every remaining check in this function (the no-go-backwards rule, the
    # prefix re-derivation, the equivocation scan, the previous_digest chain
    # link) treats `previous` as the truth about what was published. Without
    # this, an attacker who can write to the database rewrites the previous
    # row's merkle_root and every one of those checks agrees with the forgery:
    # the root no longer has to match the log, only itself.
    #
    # `previous` is the newest row of ANY scheme, deliberately. Authenticating
    # only the signer's own scheme would mean a v2 row could never be caught by
    # F1, and a v2 row is what an attacker writes to stay invisible.
    if previous is not None:
        auth = verify_previous_checkpoint(previous, trusted_keys)
        if not auth.ok:
            logger.error(
                f"REFUSING: the newest stored checkpoint ({newest.key_id}, size "
                f"{previous.checkpoint.tree_size}) is not authentic: {auth.reason}"
            )
            return _refusal(
                auth.code or REFUSAL_PREVIOUS_SIGNATURE_INVALID,
                tree_size=tree_size,
                previous_tree_size=previous.checkpoint.tree_size,
                previous_key_id=previous.key_id,
                detail_problem=auth.reason,
            )

    # F3 pre-sign. The signer's key must be published in the trusted set, in
    # bounds for this size and time, and publicly verifiable. A pasted raw key
    # or a changed derivation silently produces a DIFFERENT key from the one the
    # operator published; every checkpoint it then signs is unverifiable by a
    # third party, and the cron reports success on every run. Checking here means
    # the run refuses instead.
    # F7: bounds are checked against the time the checkpoint will claim to have
    # been signed, not only against "now". Verifying a checkpoint checks its own
    # timestamp against the key's window; if signing did not, a key could be used
    # to sign a checkpoint dated outside its own validity, and only the
    # verification side would object -- after the fact, to a row that is already
    # published. What this does NOT buy, stated plainly: a key holder chooses the
    # timestamp it signs with, so it can backdate inside the window freely. The
    # bound stops a key being used long past its expiry; it cannot stop a key
    # lying about when it was used. Closing that needs an independent anchor time
    # (DECISIONS.md phase 2, external witnessing), which is out of scope here.
    signing_time = (timestamp or datetime.now(UTC)).astimezone(UTC)
    signer_verdict = _check_signer_key(signer, trusted_keys, tree_size, signing_time)
    if isinstance(signer_verdict, SigningResult):
        return signer_verdict

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

    # F15: the chain must run forwards in time. A checkpoint dated before its
    # predecessor makes the watchdog read the newest row as older than it is and
    # makes a reader walking previous_digest see time run backwards.
    if newest is not None and signing_time < _as_utc(newest.timestamp):
        logger.error(
            f"REFUSING: the new checkpoint is dated {signing_time.isoformat()} but the "
            f"newest published one is dated {_as_utc(newest.timestamp).isoformat()}"
        )
        return _refusal(
            REFUSAL_TIMESTAMP_BACKDATED,
            tree_size=tree_size,
            previous_tree_size=newest.tree_size,
            previous_timestamp=_as_utc(newest.timestamp).isoformat(),
            attempted_timestamp=signing_time.isoformat(),
        )

    # Re-derive rather than trust: read the leaves back and recompute the root
    # over the first tree_size of them. A checkpoint whose root disagrees with
    # the log as it stands now is a checkpoint of a history that no longer
    # exists, and tlog-checkpoint forbids signing one. `entries` was read and
    # shape-checked above; it is not read again here.
    leaves = [entry.leaf_hash for entry in entries[:tree_size]]
    derived_root = root_for_scheme(leaves, SIGNER_TREE_SCHEME)
    derived_hex = derived_root.hex()

    published_at_size = await _checkpoints_at_size(session, tree_size, skip_ids=skip_ids)
    if published_at_size:
        # Each row is re-derived under its OWN scheme, not the signer's. That is
        # the only way an equivocation scan spanning two schemes is sound: a
        # legacy CT row legitimately disagrees with an RFC-6962 re-derivation of
        # the same leaves, so comparing it to `derived_hex` would refuse on every
        # honest legacy row, while skipping it entirely would let a forged legacy
        # row hide. Re-deriving per-row says the honest row agrees with itself
        # and the forged row does not.
        disagreeing = [
            row
            for row in published_at_size
            if row.merkle_root != root_for_scheme(leaves, _row_scheme(row)).hex()
        ]
        if disagreeing:
            logger.error(
                f"REFUSING: tree_size {tree_size} is already published with root "
                f"{disagreeing[0].merkle_root[:12]} but the log now derives "
                f"{derived_hex[:12]} under {_row_scheme(disagreeing[0])}. "
                "This is equivocation and must not be signed."
            )
            return _refusal(
                REFUSAL_EQUIVOCATION,
                tree_size=tree_size,
                published_root=disagreeing[0].merkle_root,
                derived_root=derived_hex,
                published_key_id=disagreeing[0].key_id,
            )
        # Idempotency is scoped to the format. A row from an older format or tree
        # scheme at this size agreeing with nothing comparable does NOT mean
        # there is a current checkpoint here: those rows are append-only and
        # cannot be rewritten, so the first checkpoint in the new format is
        # published at the same size as the last one in the old format, and the
        # pair is chained by previous_digest. Treating the old row as
        # already-signed would leave the log permanently on a superseded scheme
        # while the cron reports success on every run.
        same_format = [
            row
            for row in published_at_size
            if _row_format(row) == SIGNER_FORMAT and _row_scheme(row) == SIGNER_TREE_SCHEME
        ]
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

    if newest_in_scheme is not None and newest_in_scheme.tree_size < tree_size:
        # Re-derive the root over the entries the newest checkpoint already
        # covers. If that no longer matches what was published, the prefix of
        # the log changed under a signed checkpoint, and tlog-checkpoint forbids
        # signing anything on top of that.
        #
        # `newest_in_scheme`, not `newest`: this is the one comparison that must
        # stay inside one scheme. A superseded-scheme root is a different number
        # over the same leaves by construction, so comparing it to an RFC-6962
        # re-derivation would refuse forever against a row that is perfectly
        # genuine. Those rows are instead covered by the per-scheme equivocation
        # scan above, which checks each one against the log under its own scheme.
        covered = [entry.leaf_hash for entry in entries[: newest_in_scheme.tree_size]]
        rederived_previous = root_for_scheme(covered, SIGNER_TREE_SCHEME).hex()
        if rederived_previous != newest_in_scheme.merkle_root:
            logger.error(
                f"REFUSING: root over the first {newest_in_scheme.tree_size} entries is now "
                f"{rederived_previous[:12]} but checkpoint {newest_in_scheme.key_id} published "
                f"{newest_in_scheme.merkle_root[:12]}. The prefix of the log changed."
            )
            return _refusal(
                REFUSAL_INCONSISTENT_HISTORY,
                tree_size=tree_size,
                previous_tree_size=newest_in_scheme.tree_size,
                previous_root=newest_in_scheme.merkle_root,
                rederived_root=rederived_previous,
            )

    # F10: a ValueError from here on is a broken chain, not a server fault. It is
    # the tamper case -- a rewritten payload, a chain that does not verify, an
    # entries argument that does not cover the tree -- and it used to leave the
    # route as a 500 with nothing in the alert table.
    try:
        checkpoint = await build_checkpoint(
            log,
            tree_size,
            timestamp=signing_time,
            format=SIGNER_FORMAT,
            origin=origin,
            previous_digest=bytes.fromhex(previous_digest) if previous_digest else None,
            key_id=signer.key_id,
            entries=entries,  # F15: one read of the log per run, not two
        )
    except ValueError as exc:
        logger.error(
            f"REFUSING: the log does not verify, so no checkpoint can be signed over it: {exc}"
        )
        return _refusal(
            REFUSAL_CHAIN_INVALID,
            tree_size=tree_size,
            detail_problem=str(exc)[:200],
        )
    if checkpoint.merkle_root.hex() != derived_hex:
        # build_checkpoint derives from the entries passed in, so a disagreement
        # means the two derivations used different leaves. It cannot be right.
        logger.error("REFUSING: build_checkpoint derived a different root than the pre-check")
        return _refusal(
            REFUSAL_INCONSISTENT_HISTORY,
            tree_size=tree_size,
            derived_root=derived_hex,
            build_root=checkpoint.merkle_root.hex(),
        )

    try:
        signed = sign_checkpoint(checkpoint, signer)
    except ValueError as exc:
        # F8: origin vs key name, or a payload key id that is not this key's.
        logger.error(f"REFUSING: this key cannot sign this checkpoint: {exc}")
        return _refusal(
            REFUSAL_ORIGIN_KEY_MISMATCH,
            tree_size=tree_size,
            key_id=getattr(signer, "key_id", None),
            origin=origin,
            detail_problem=str(exc)[:200],
        )


    # F3 post-sign. Verify our own output before it is stored, against the same
    # trusted key set the pre-sign check admitted. This closes the gap where a
    # derived key silently differs from the published one: the checkpoint would
    # be stored, the run would report success, and every third party would render
    # an honest "unverified signature" forever with nothing in the logs saying
    # why. Refusing here means the stored history stays verifiable-by-construction.
    if not verify_checkpoint(signed, signer):
        logger.error(
            "REFUSING: the signature this signer just produced does not verify "
            f"against its own key {signed.key_id}. Refusing to store it."
        )
        return _refusal(
            REFUSAL_SIGNER_VERIFY_FAILED,
            tree_size=tree_size,
            key_id=signed.key_id,
            detail_problem="signature failed verification against the signing key",
        )
    self_check = verify_previous_checkpoint(signed, trusted_keys)
    if not self_check.ok:
        # It verifies against the key itself but not against the PUBLISHED key:
        # the two disagree, which is precisely the mis-derived-key case.
        logger.error(
            f"REFUSING: the new checkpoint does not verify against the published "
            f"key set: {self_check.reason}. The signing key and the published "
            "public key are not the same key."
        )
        return _refusal(
            REFUSAL_SIGNER_KEY_MISMATCH,
            tree_size=tree_size,
            key_id=signed.key_id,
            detail_problem=self_check.reason,
        )

    await save_checkpoint(session, signed)
    published_digest = checkpoint_digest(signed)
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
        detail={
            "key_id": signed.key_id,
            "algorithm": signed.algorithm,
            "origin": origin,
            "format": SIGNER_FORMAT,
            "tree_scheme": SIGNER_TREE_SCHEME,
            "entry_timestamps": (
                checkpoint.entry_timestamps.hex() if checkpoint.entry_timestamps else None
            ),
            "superseded_scheme_checkpoints": superseded,
            # What the operator must record in transparency_signed_head for the
            # next run's F2 floor. Emitted rather than written anywhere by the
            # signer, because a value this process can write is a value an
            # attacker with database write access can also move.
            "published_head": _published_head_value(tree_size, published_digest),
        },
    )


def _log_shape_problem(entries: Sequence[LogEntry], tree_size: int) -> str | None:
    """Why the log's shape cannot be checkpointed, or None if it can.

    Three separate defects produce the same symptom downstream, so they are
    named separately here where they can still be told apart:

    - a count mismatch: log.size() reports max(index)+1, so any gap or missing
      row makes the reported tree_size larger than the rows present. Signing a
      root over fewer leaves than the tree_size it is labelled with produces an
      artifact that says one thing and means another.
    - a non-contiguous index: verify_chain requires entry.index == position, so
      a gap or a duplicate fails it -- but only after build_checkpoint has
      already re-read the log and raised ValueError, which the cron reports as
      a 500 rather than a refusal.
    - a poisoned row claiming an index far beyond the log: this is the "one
      INSERT permanently halts signing" case. The refusal is the same, but the
      detail carries the index so the operator knows which row to quarantine.
    """
    if len(entries) != tree_size:
        return (
            f"log.size() reported {tree_size} but {len(entries)} entries are "
            "present (a deleted row leaves a gap that max(index)+1 hides)"
        )
    for position, entry in enumerate(entries):
        if entry.index != position:
            return (
                f"entry at position {position} carries index {entry.index}; "
                "indices must be contiguous from 0"
            )
    return None


@dataclass(frozen=True)
class AuthenticationVerdict:
    """The outcome of authenticating one checkpoint row.

    `code` is the refusal reason code to publish when ok is False, and None when
    ok is True. Carrying the code on the verdict (rather than mapping a prose
    string to one at the call site) is what lets a test assert that a tampered
    root produces `previous_signature_invalid` specifically, and not merely that
    something refused.
    """

    ok: bool
    reason: str = ""
    code: str | None = None


def verify_previous_checkpoint(
    previous: SignedCheckpoint,
    trusted_keys: Mapping[str, TrustedKey] | None,
) -> AuthenticationVerdict:
    """Authenticate a stored checkpoint row against the published key set.

    This is F1, and it is the only check in the signer that does not compare two
    values the same attacker can edit. Everything else in sign_next_checkpoint
    compares the log to the checkpoint table or the table to itself, and an
    attacker with database write access rewrites both sides together; the
    detached signature covers every signed field, so tampering with the stored
    merkle_root, chain_hash, tree_size, timestamp or previous_digest breaks it
    even when the row is otherwise left intact.

    Deliberately fails CLOSED on a missing trusted set, a missing key, an
    unverifiable algorithm (HMAC proves nothing to a third party, see
    keys.load_trusted_keys), and an out-of-bounds key. A refusal here means "I
    will not build on this row", which is the only safe direction: the
    alternative is signing on top of an unauthenticated history, which is the
    defect.
    """
    key = (trusted_keys or {}).get(previous.key_id)
    if key is None:
        return AuthenticationVerdict(
            False,
            f"no published key for the checkpoint's key_id {previous.key_id!r}; "
            "refusing to trust a row nobody can verify",
            code=REFUSAL_PREVIOUS_KEY_UNKNOWN,
        )
    verifier = verifier_for(previous, {previous.key_id: key})
    if verifier is None:
        # verifier_for returns None for an unusable key, a mismatched algorithm,
        # and a checkpoint outside the key's validity window. Distinguish the
        # bounds case so the alert says "rotate the key" rather than "publish a
        # key", which are different operator actions.
        ok, reason = key.is_within_bounds(
            previous.checkpoint.tree_size, previous.checkpoint.timestamp
        )
        if not ok:
            return AuthenticationVerdict(
                False, f"published key {key.key_id!r} {reason}",
                code=REFUSAL_PREVIOUS_KEY_OUT_OF_BOUNDS,
            )
        return AuthenticationVerdict(
            False,
            f"published key {key.key_id!r} cannot verify this checkpoint "
            "(algorithm mismatch, or ed25519 is unavailable in this process)",
            code=REFUSAL_PREVIOUS_KEY_OUT_OF_BOUNDS,
        )
    if not verify_checkpoint(previous, verifier):
        return AuthenticationVerdict(
            False,
            f"the stored signature does not verify against published key {key.key_id!r}; "
            "the row's signed fields have been altered",
            code=REFUSAL_PREVIOUS_SIGNATURE_INVALID,
        )
    return AuthenticationVerdict(True, "verified against a published key")


def _check_against_head(
    newest: TransparencyCheckpoint | None,
    previous_digest: str | None,
    head: TrustedHead | None,
    *,
    genesis_confirmed: bool,
) -> SigningResult | None:
    """F2. Compare the database against the externally recorded head.

    Returns a refusal to return, or None to continue. Four states:

    1. No head configured, no row in the table. Only a genuine genesis gets
       here, and only with genesis_confirmed. Without the flag this refuses,
       because RLS hiding every row, a truncated table, and a first run are
       indistinguishable from inside the database -- which is exactly the defect.
    2. A head is configured and the table's newest row does not cover it. The
       published history is missing rows the operator recorded. Refuse.
    3. A head is configured, a row exists at the head's tree_size, and its
       digest is not the head's digest. The row at the published size is not the
       one that was published. Refuse.
    4. The table is at or past the head. Fine: the head is a floor, not a
       ceiling, and the signer is expected to be ahead of it between advances.
    """
    if newest is None:
        if head is not None:
            logger.error(
                f"REFUSING: transparency_signed_head records a published checkpoint "
                f"covering {head.tree_size} entries, but transparency_checkpoints "
                "holds no rows at all. Deleted, truncated, or hidden by RLS."
            )
            return _refusal(
                REFUSAL_HEAD_AHEAD_OF_LOG,
                head_tree_size=head.tree_size,
                head_digest=head.digest,
                checkpoint_rows=0,
            )
        if not genesis_confirmed:
            logger.error(
                "REFUSING: no checkpoint has ever been published and genesis is not "
                "confirmed. Set transparency_genesis_confirmed=true to sign the first "
                "checkpoint deliberately, or record the head of the last published one."
            )
            return _refusal(
                REFUSAL_GENESIS_UNCONFIRMED,
                tree_size=0,
                detail_problem="no published checkpoint and no genesis confirmation",
            )
        return None

    if head is None:
        # Rows exist but no external record of them. The intra-table checks still
        # apply (F1 authenticates the newest row), but rollback detection needs
        # the head. Say so loudly rather than quietly proceeding: this is a real
        # reduction in what the signer can guarantee.
        logger.warning(
            "transparency_signed_head is not configured while %d checkpoint row(s) "
            "are published. The previous checkpoint is still signature-verified, but "
            "rollback or deletion of published rows cannot be detected until a head "
            "is recorded outside this database.",
            len([newest]),
        )
        return None

    if newest.tree_size < head.tree_size:
        logger.error(
            f"REFUSING: transparency_signed_head records a published checkpoint "
            f"covering {head.tree_size} entries, but the newest stored checkpoint "
            f"covers only {newest.tree_size}. Published checkpoints were deleted or "
            "truncated."
        )
        return _refusal(
            REFUSAL_HEAD_AHEAD_OF_LOG,
            head_tree_size=head.tree_size,
            head_digest=head.digest,
            newest_tree_size=newest.tree_size,
            newest_key_id=newest.key_id,
        )

    if newest.tree_size == head.tree_size and previous_digest != head.digest:
        logger.error(
            f"REFUSING: a checkpoint covering {head.tree_size} entries is stored, "
            f"but its digest {str(previous_digest)[:12]} is not the recorded head "
            f"{head.digest[:12]}. The row at the published size is not the one that "
            "was published."
        )
        return _refusal(
            REFUSAL_HEAD_MISMATCH,
            head_tree_size=head.tree_size,
            head_digest=head.digest,
            stored_digest=previous_digest,
            newest_key_id=newest.key_id,
        )

    return None


def _check_signer_key(
    signer: Signer,
    trusted_keys: Mapping[str, TrustedKey] | None,
    tree_size: int,
    signing_time: datetime | None = None,
) -> SigningResult | None:
    """F3 pre-sign. The signer's key must be published and in bounds.

    Returns a refusal to return, or None to continue. The failure this exists for
    is quiet and total: TRANSPARENCY_SIGNING_KEY is hashed down to 32 bytes, so
    pasting a raw 32-byte key instead of a seed produces a completely different
    signing key, nothing compares the two, and the signer then publishes a
    checkpoint stream that no third party can verify -- while every run reports
    success. Comparing the signer's key_id and public key against the published
    set here turns that into a refusal on the first run.
    """
    keys = trusted_keys or {}
    key = keys.get(signer.key_id)
    if key is None:
        logger.error(
            f"REFUSING: the signing key {signer.key_id!r} is not in the published "
            f"trusted key set ({len(keys)} key(s)). A key nobody can verify is not a "
            "signed log."
        )
        return _refusal(
            REFUSAL_SIGNER_KEY_UNTRUSTED,
            tree_size=tree_size,
            key_id=signer.key_id,
            trusted_key_ids=sorted(keys),
        )
    if key.algorithm != signer.algorithm:
        logger.error(
            f"REFUSING: the signing key {signer.key_id!r} uses {signer.algorithm} but "
            f"the published key for that id is {key.algorithm}."
        )
        return _refusal(
            REFUSAL_SIGNER_KEY_UNTRUSTED,
            tree_size=tree_size,
            key_id=signer.key_id,
            detail_problem="algorithm disagreement with the published key",
        )
    # Key IDENTITY (is this the key that was published?) is deliberately NOT
    # checked here. It is checked after signing, by verify_previous_checkpoint
    # against the published key's bytes, and it has to be there: a signer that
    # cannot expose a public key -- or one whose public_key_bytes() raises --
    # skips an identity comparison done here, and that is precisely the shape a
    # mis-derived key takes. Splitting it this way also keeps one guard per
    # concern. A comparison that exists in both places is a comparison where
    # removing either one still passes every test, which is how a guard ends up
    # untested: this file's mutation table is the evidence that neither did.
    # F7: bounds are enforced at SIGNING, against the moment the checkpoint will
    # claim, and against now as well. Verification already refuses a checkpoint
    # outside the window; without this, a run could publish a dated checkpoint the
    # verifier will reject, and the operator would only find out from a third
    # party. It still does not stop a key holder backdating inside the window --
    # see the note in sign_next_checkpoint.
    moment = signing_time or datetime.now(UTC)
    ok, reason = key.is_within_bounds(tree_size, moment)
    if not ok:
        logger.error(f"REFUSING: the signing key {signer.key_id!r} {reason}")
        return _refusal(
            REFUSAL_SIGNER_KEY_OUT_OF_BOUNDS,
            tree_size=tree_size,
            key_id=signer.key_id,
            detail_problem=reason,
        )
    return None


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
        moment = moment.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return (reference - moment).total_seconds() / 3600.0


async def checkpoint_lag(session: AsyncSession) -> tuple[int | None, int | None]:
    """(newest published tree_size, current log size) for the live scheme.

    F13: the difference between the two is what tells an IDLE log from a DEAD
    signer. A log with no new entries since the last checkpoint has nothing to
    sign, so an old checkpoint there is the log being quiet, not the cron having
    stopped. Reporting both as "unhealthy" trains the operator to ignore the
    watchdog, which is the opposite of a dead man's switch.
    """
    newest = await _newest_checkpoint(session, scheme=SIGNER_TREE_SCHEME)
    result = await session.execute(text("select count(*) from merkle_log_entries"))
    log_size = int(result.scalar() or 0)
    return (newest.tree_size if newest is not None else None), log_size


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


def watchdog_state(
    age_hours: float | None,
    *,
    max_interval_hours: float,
    published_tree_size: int | None = None,
    log_size: int | None = None,
    age_readable: bool = True,
) -> tuple[bool, str, str]:
    """(healthy, state, verdict) -- the same answer as watchdog_verdict, plus
    WHY, distinguishing the states an operator has to act on differently (F13).

        ok       a fresh checkpoint exists and covers the whole log
        idle     the log is quiet: the newest checkpoint covers every entry, so
                 there is nothing new to sign. Not unhealthy. An old checkpoint
                 here means no articles have been published, not that the cron
                 died, and conflating the two is what makes people ignore this
                 endpoint.
        stale    the checkpoint is past the interval AND entries are waiting to
                 be signed. This is the real "the signer stopped" signal.
        never    nothing has ever been published under the live tree scheme
        unknown  the age could not be read (F10: never guessed at from a
                 transaction that has already aborted)
    """
    if age_hours is None:
        # `never` and `unknown` are both age_hours=None, and collapsing them is
        # its own lie: "nothing has ever been published" is an operator action
        # (publish a genesis checkpoint), "the age could not be read" is an
        # infrastructure problem (fix the database). Before age_readable existed
        # this function's own docstring advertised `unknown` and could never
        # return it.
        if not age_readable:
            return False, "unknown", (
                "the newest checkpoint's age could not be read, so the log's "
                "freshness is unmeasured; it is NOT assumed to be never-signed"
            )
        return False, "never", "no checkpoint has been published yet"
    covers_log = (
        published_tree_size is not None and log_size is not None and published_tree_size >= log_size
    )
    if age_hours > max_interval_hours and not covers_log:
        return False, "stale", (
            f"newest checkpoint is {age_hours:.1f}h old, past the "
            f"{max_interval_hours:.1f}h interval, and {log_size} entries are "
            f"waiting for one"
        )
    if covers_log:
        return True, "idle", (
            f"log is idle: the newest checkpoint covers all {log_size} entries, so "
            f"there is nothing new to sign (checkpoint is {age_hours:.1f}h old)"
        )
    return True, "ok", f"newest checkpoint is {age_hours:.1f}h old"


async def record_alert(
    session: AsyncSession,
    kind: str,
    *,
    detail: dict[str, Any] | None = None,
    min_interval_seconds: float = ALERT_DEDUPE_SECONDS,
) -> str:
    """Write one alert row. Returns "recorded", "suppressed", or "failed".

    Three things changed here (F10), and each was a way a refusal could vanish:

    - It no longer returns None and says nothing. The caller puts the returned
      status in the response body, so "refused, and the alert did not get
      written" is visible to whoever ran the cron instead of being a silent hole
      in the evidence. A failed write is logged at ERROR with the exception,
      because a swallowed alert is indistinguishable from no alert.
    - It no longer swallows the failure. The row is still best-effort -- losing
      it to a database problem must not turn a clean refusal into a stack trace
      -- but the failure is REPORTED (returned and logged), not hidden.
    - Repeats of the same kind inside min_interval_seconds are suppressed
      instead of appended. lock_held is the case that matters: a second cron
      fire every minute while the first is stuck writes a row a minute, and the
      operator reads the newest five alerts on the watchdog, so the flood hides
      every other signal. Suppression is reported too, so "suppressed" is
      distinguishable from "recorded" and from "failed".
    """
    from src.transparency.alerts import TransparencyAlert  # local import: optional table

    try:
        if min_interval_seconds > 0:
            recent = await session.execute(
                select(TransparencyAlert.created_at)
                .where(TransparencyAlert.kind == kind)
                .order_by(TransparencyAlert.created_at.desc())
                .limit(1)
            )
            newest_created = recent.scalar()
            if newest_created is not None:
                created = newest_created
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                gap = (datetime.now(UTC) - created).total_seconds()
                if gap < min_interval_seconds:
                    logger.info(
                        f"Suppressing a repeat {kind!r} alert: one was recorded "
                        f"{gap:.0f}s ago (dedupe window {min_interval_seconds:.0f}s)"
                    )
                    return "suppressed"
        session.add(TransparencyAlert(kind=kind, detail=detail or {}))
        await session.flush()
    except Exception as exc:  # table missing (migration not applied), etc.
        logger.error(
            f"Could not record transparency alert {kind!r}: {type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return "failed"
    return "recorded"


__all__ = [
    "ADVISORY_LOCK_KEY",
    "AuthenticationVerdict",
    "ALERT_DEDUPE_SECONDS",
    "REFUSAL_ALREADY_SIGNED",
    "REFUSAL_CHAIN_INVALID",
    "REFUSAL_EMPTY_LOG",
    "REFUSAL_EQUIVOCATION",
    "REFUSAL_GENESIS_UNCONFIRMED",
    "REFUSAL_HEAD_AHEAD_OF_LOG",
    "REFUSAL_HEAD_MISMATCH",
    "REFUSAL_INCONSISTENT_HISTORY",
    "REFUSAL_LOCK_HELD",
    "REFUSAL_ORIGIN_KEY_MISMATCH",
    "REFUSAL_LOG_SHAPE",
    "REFUSAL_LOG_SMALLER",
    "REFUSAL_PREVIOUS_KEY_OUT_OF_BOUNDS",
    "REFUSAL_PREVIOUS_KEY_UNKNOWN",
    "REFUSAL_PREVIOUS_SIGNATURE_INVALID",
    "REFUSAL_SIGNER_KEY_MISMATCH",
    "REFUSAL_SIGNER_KEY_OUT_OF_BOUNDS",
    "REFUSAL_SIGNER_KEY_UNTRUSTED",
    "REFUSAL_SIGNER_VERIFY_FAILED",
    "REFUSAL_TIMESTAMP_BACKDATED",
    "SIGNER_FORMAT",
    "SIGNER_TREE_SCHEME",
    "SigningResult",
    "TrustedHead",
    "checkpoint_age_hours",
    "checkpoint_lag",
    "quarantined_checkpoint_ids",
    "superseded_checkpoint_count",
    "parse_trusted_head",
    "record_alert",
    "sign_next_checkpoint",
    "verify_previous_checkpoint",
    "watchdog_state",
    "watchdog_verdict",
]
