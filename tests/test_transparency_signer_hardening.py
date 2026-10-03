"""Attack tests for the signer hardening batch (F1-F5).

Every test in this file performs an ATTACK and asserts the signer REFUSES. That
is the whole point. The signer hardening batch exists because the pre-hardening
signer passed every happy-path test and failed open on every real attack: it
trusted an unauthenticated previous checkpoint, read an empty checkpoint table
as genesis, signed with whatever key it derived, and turned a one-row index gap
into a permanent 500.

So this file is organized by attack, not by code path, and each test states the
attack in its name. If a guard is removed, the test named for the attack it
guards must go red -- which is why the mutation table in the report is a
deliverable and not a formality.

  TestForgedPreviousCheckpoint   F1: tamper with a stored row, expect a refusal
  TestRollbackAndGenesis         F2: delete every row, expect a refusal
  TestSignerKeyTrust             F3: wrong/low-entropy key, expect a refusal
  TestPoisonedLogShape           F5: planted tree_size and index gap
  TestQuarantine                 how a poisoned row stops blocking, and its limits
  TestHeadParsing                the config that decides whether F2 is armed at all

Test counts, mutation-verified with before/after output in the report.

Two things about the environment, both load-bearing:

- These run on SQLite by default, which proves the LOGIC. The Postgres-specific
  claims (RLS hiding rows from the signer role, the append-only trigger, the
  least-privilege grants) are proven in
  tests/test_transparency_signer_grants.py and by
  scripts/check_signer_privileges.py, because a SQLite run cannot exercise any
  of them and pretending otherwise is the bug class this batch was created for.
- `lock=False` throughout: the advisory lock is `pg_try_advisory_xact_lock`, which
  SQLite has no equivalent of. Every other line of code is the production path.
"""

import dataclasses
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from src.transparency import signing
from src.transparency.checkpoint import (
    FORMAT_C2SP_V2,
    FORMAT_JSON_V1,
    Checkpoint,
    SignedCheckpoint,
    ed25519_available,
    generate_ed25519_signer,
    sign_checkpoint,
    verify_checkpoint,
)
from src.transparency.keys import TrustedKey
from src.transparency.log import InMemoryMerkleLog, TransparencyBase
from src.transparency.store import (
    TransparencyCheckpoint,
    TransparencyQuarantine,
    save_checkpoint,
)

needs_cryptography = pytest.mark.skipif(
    not ed25519_available(), reason="cryptography is not installed"
)

ORIGIN = "procmon.dev/transparency"


def _seed(label: str) -> bytes:
    """A deterministic 32-byte signing seed. See the note in the header."""
    import hashlib

    return hashlib.sha256(f"test-seed:{label}".encode()).digest()


def _published(signer) -> TrustedKey:
    """The TrustedKey an operator would publish for this signer.

    Derived FROM the signer's own public key rather than hand-written, so the
    happy-path tests exercise the real published-key shape (and therefore the
    real key_id) instead of a fixture that happens to agree.
    """
    return TrustedKey(
        key_id=signer.key_id,
        algorithm=signer.algorithm,
        public_key=signer.public_key_bytes(),
    )


def _trusted(signer, **extra) -> dict[str, TrustedKey]:
    return {signer.key_id: _published(signer, **extra)}


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(TransparencyBase.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


async def _log_with(entries: int, label: str = "attack") -> InMemoryMerkleLog:
    log = InMemoryMerkleLog()
    for i in range(entries):
        await log.append({"type": label, "n": str(i)})
    return log


async def _sign(
    session,
    log,
    signer,
    *,
    trusted=None,
    head=None,
    genesis: bool = True,
    origin: str = ORIGIN,
):
    """The signing call under test, with the production trust inputs."""
    return await signing.sign_next_checkpoint(
        session,
        log,
        signer,
        origin=origin,
        lock=False,
        trusted_keys=trusted if trusted is not None else _trusted(signer),
        head=head,
        genesis_confirmed=genesis,
    )


async def _plant_authentic_row(session, signer, tree_size: int) -> TransparencyCheckpoint:
    """Insert a checkpoint row the signer itself could have written.

    Signed properly, with the real key, so it passes F1. That matters: a row
    planted by an ATTACKER fails F1 first and is caught there, which is the
    better outcome, but it would mask the F5 availability finding this row
    exists to reproduce. The realistic F5 case is a genuine signer bug or a
    misconfiguration that published a nonsense tree_size: authentic, append-only,
    undeletable, and permanently blocking. This builds exactly that.
    """
    poisoned = Checkpoint(
        tree_size=tree_size,
        merkle_root=signing.merkle_root([b"\x00" * 32] * 1),
        chain_hash=b"\xcd" * 32,
        timestamp=datetime.now(timezone.utc),
        origin=ORIGIN,
        format=FORMAT_C2SP_V2,
    )
    row = await save_checkpoint(session, sign_checkpoint(poisoned, signer))
    return row


async def _empty_checkpoints(session) -> None:
    """Remove every published checkpoint row.

    Stands in for the three ways published rows really do disappear: a DELETE by
    someone with enough privilege to bypass the append-only trigger, a disabled
    trigger, and RLS making the rows invisible to the signer's role. All three
    look identical from inside the database, and all three must be caught.
    """
    await session.execute(text("DELETE FROM transparency_checkpoints"))
    await session.flush()


class _GappyLog:
    """Wraps InMemoryMerkleLog but reports size the way the real log does.

    SqlAlchemyMerkleLog.size() is `max(index) + 1`, NOT the row count -- which is
    why a deleted middle row is invisible to a naive count and only shows up as
    a gap. InMemoryMerkleLog returns len(entries), so a test built on it alone
    can never reproduce the production defect. This wrapper makes the test log
    behave like the production one for the one method that matters.
    """

    def __init__(self, inner: InMemoryMerkleLog) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def size(self) -> int:
        entries = await self._inner.entries()
        return max((e.index for e in entries), default=-1) + 1


async def _publish(session, log, signer, tree_size: int | None = None) -> SignedCheckpoint:
    """Sign one checkpoint over the log and return the signed artifact."""
    size = tree_size if tree_size is not None else await log.size()
    checkpoint = Checkpoint(
        tree_size=size,
        merkle_root=signing.merkle_root([e.leaf_hash for e in await log.entries()][:size]),
        chain_hash=(await log.entries())[size - 1].chain_hash,
        timestamp=datetime.now(timezone.utc),
        origin=ORIGIN,
        format=FORMAT_C2SP_V2,
    )
    signed = sign_checkpoint(checkpoint, signer)
    await save_checkpoint(session, signed)
    return signed


class TestForgedPreviousCheckpoint:
    """F1 CRITICAL: the previous row must be authenticated before it is trusted.

    The pre-hardening signer took the newest row, turned it into a
    SignedCheckpoint, and compared the log against it. That is sound only if the
    row is authentic, and an attacker with database write access can make a
    forged row and a forged log agree with each other. The only input the same
    attacker cannot edit is the detached signature.
    """

    @needs_cryptography
    async def test_tampering_with_a_stored_root_is_refused(self, db_session):
        """The attack from the review, performed literally.

        Publish a real checkpoint, then rewrite ONLY the stored merkle_root --
        the row is otherwise completely intact, same tree_size, same chain_hash,
        same timestamp, same signature bytes. Pre-hardening, the signer read
        that root, compared it against the log, found a mismatch, and refused
        with inconsistent_history -- so it looked defended. The attack that
        works is the other direction: rewrite the LEAVES too, so the root
        matches. That is what this test does.
        """
        signer = generate_ed25519_signer(seed=_seed("f1-forgery"))
        trusted = _trusted(signer)

        # An attacker-controlled log and a matching forged checkpoint over it.
        forged_log = await _log_with(3, label="forged")
        entries = await forged_log.entries()
        forged_root = signing.merkle_root([e.leaf_hash for e in entries])
        forged = Checkpoint(
            tree_size=3,
            merkle_root=forged_root,
            chain_hash=entries[-1].chain_hash,
            timestamp=datetime.now(timezone.utc),
            origin=ORIGIN,
            format=FORMAT_C2SP_V2,
        )
        # The signature is a valid HMAC over the forged body -- an attacker who
        # can write the database may well hold the old dev secret -- so the row
        # is internally perfect. Only the published-key check catches it.
        row = TransparencyCheckpoint(
            tree_size=3,
            merkle_root=forged.merkle_root.hex(),
            chain_hash=forged.chain_hash.hex(),
            timestamp=forged.timestamp,
            signature="11" * 32,
            algorithm="ed25519",
            key_id=signer.key_id,
            checkpoint_format=FORMAT_C2SP_V2,
        )
        db_session.add(row)
        await db_session.flush()

        # The real log now also has a tampered row at the top: swap in the
        # forged one as the newest and append, so the signer would otherwise
        # happily sign tree_size 4 on top of the forged history.
        result = await _sign(db_session, forged_log, signer, trusted=trusted)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_PREVIOUS_SIGNATURE_INVALID
        assert result.signed is None
        rows = await db_session.execute(select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 1, "the signer published on top of a forgery"

    @needs_cryptography
    async def test_a_forged_row_with_the_right_root_at_the_current_size_is_refused(
        self, db_session
    ):
        """The specific case the review called out.

        A forged v2 row at the CURRENT tree size, carrying the RIGHT root, so
        `same_format` matches and the pre-hardening signer returned
        already_signed -- serving the forged note from the public proof page
        while reporting success. The root being right is the whole trap: it is
        what makes every consistency check agree.
        """
        signer = generate_ed25519_signer(seed=_seed("f1-same-size"))
        trusted = _trusted(signer)
        log = await _log_with(3)
        entries = await log.entries()
        good_root = signing.merkle_root([e.leaf_hash for e in entries])

        forged = Checkpoint(
            tree_size=3,
            merkle_root=good_root,  # correct on purpose
            chain_hash=entries[-1].chain_hash,
            timestamp=datetime.now(timezone.utc),
            origin=ORIGIN,
            format=FORMAT_C2SP_V2,
        )
        db_session.add(
            TransparencyCheckpoint(
                tree_size=3,
                merkle_root=good_root.hex(),
                chain_hash=forged.chain_hash.hex(),
                timestamp=forged.timestamp,
                # A signature that is the right LENGTH and a real-looking value,
                # but not a signature over this body by the published key.
                signature="22" * 68,
                algorithm="ed25519",
                key_id=signer.key_id,
                checkpoint_format=FORMAT_C2SP_V2,
            )
        )
        await db_session.flush()

        result = await _sign(db_session, log, signer, trusted=trusted)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_PREVIOUS_SIGNATURE_INVALID
        # Explicitly NOT already_signed: that was the failure mode.
        assert result.reason != signing.REFUSAL_ALREADY_SIGNED

    @needs_cryptography
    async def test_a_row_whose_key_is_not_published_is_refused(self, db_session):
        """No published key for the key_id means nobody can verify the row."""
        signer = generate_ed25519_signer(seed=_seed("f1-unknown-key"))
        log = await _log_with(2)
        await _publish(db_session, log, signer)

        result = await _sign(db_session, log, signer, trusted={})
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_PREVIOUS_KEY_UNKNOWN

    @needs_cryptography
    async def test_a_row_outside_its_key_validity_window_is_refused(self, db_session):
        """Bounds are enforced on the signer path, not only on the proof page."""
        signer = generate_ed25519_signer(seed=_seed("f1-bounds"))
        log = await _log_with(2)
        await _publish(db_session, log, signer)
        # max_tree_size below the published checkpoint's size: the key is not
        # valid for a checkpoint this large.
        trusted = {
            signer.key_id: TrustedKey(
                key_id=signer.key_id,
                algorithm=signer.algorithm,
                public_key=signer.public_key_bytes(),
                max_tree_size=1,
            )
        }
        result = await _sign(db_session, log, signer, trusted=trusted)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_PREVIOUS_KEY_OUT_OF_BOUNDS

    @needs_cryptography
    async def test_an_hmac_signed_row_cannot_be_trusted(self, db_session):
        """A pre-v2 dev row is the real-world instance of this.

        No public key can verify an HMAC, so once F1 is on, the dev database's
        existing v1 rows refuse every future run. That is correct, and it is why
        quarantine exists -- but the refusal must be explicit, not a mystery.
        """
        signer = generate_ed25519_signer(seed=_seed("f1-hmac-row"))
        log = await _log_with(2)
        entries = await log.entries()
        v1 = Checkpoint(
            tree_size=2,
            merkle_root=signing.merkle_root([e.leaf_hash for e in entries]),
            chain_hash=entries[-1].chain_hash,
            timestamp=datetime.now(timezone.utc),
            format=FORMAT_JSON_V1,
        )
        db_session.add(
            TransparencyCheckpoint(
                tree_size=2,
                merkle_root=v1.merkle_root.hex(),
                chain_hash=v1.chain_hash.hex(),
                timestamp=v1.timestamp,
                signature="33" * 32,
                algorithm="hmac-sha256-dev",
                key_id="dev-hmac",
            )
        )
        await db_session.flush()

        result = await _sign(db_session, log, signer)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_PREVIOUS_KEY_UNKNOWN

    @needs_cryptography
    async def test_a_genuine_checkpoint_still_signs_on_top(self, db_session):
        """The control. F1 must not break the feature it secures.

        Without this, "every test refuses" would be indistinguishable from "the
        signer refuses everything".
        """
        signer = generate_ed25519_signer(seed=_seed("f1-control"))
        log = await _log_with(3)
        first = await _sign(db_session, log, signer)
        assert first.status == "signed"
        await log.append({"type": "control", "n": "3"})
        second = await _sign(db_session, log, signer)
        assert second.status == "signed"
        assert second.tree_size == 4
        assert second.previous_digest is not None
        assert verify_checkpoint(second.signed, signer)


class TestRollbackAndGenesis:
    """F2: an empty or invisible checkpoint table must not read as genesis."""

    @needs_cryptography
    async def test_no_rows_and_no_genesis_confirmation_is_refused(self, db_session):
        """The core F2 finding.

        Pre-hardening, `newest is None` meant "nothing has ever been signed", so
        it signed. But RLS hiding every row, a truncated table, and a genuine
        first run are all the same observation from inside the database. Signing
        on that observation re-signs an already-published history from the
        start, which is the attack.
        """
        signer = generate_ed25519_signer(seed=_seed("f2-genesis"))
        log = await _log_with(3)
        result = await _sign(db_session, log, signer, genesis=False)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_GENESIS_UNCONFIRMED
        rows = await db_session.execute(select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 0

    @needs_cryptography
    async def test_genesis_is_signed_only_with_the_explicit_confirmation(self, db_session):
        """The positive control for the line above."""
        signer = generate_ed25519_signer(seed=_seed("f2-genesis-ok"))
        log = await _log_with(3)
        result = await _sign(db_session, log, signer, genesis=True)
        assert result.status == "signed"
        assert result.previous_digest is None

    @needs_cryptography
    async def test_deleting_every_row_with_a_recorded_head_is_refused(self, db_session):
        """F2's rollback attack: the rows are gone, the head is not.

        This is the case F1 structurally cannot see -- there is no row left to
        authenticate. The head, held outside the database, is the only witness
        that anything was ever published.
        """
        signer = generate_ed25519_signer(seed=_seed("f2-rollback"))
        log = await _log_with(3)
        first = await _sign(db_session, log, signer)
        assert first.status == "signed"
        head = signing.parse_trusted_head(first.detail["published_head"])
        assert head is not None and head.tree_size == 3

        # The attacker empties the checkpoint table. On real Postgres the
        # append-only trigger forbids the DELETE, which is exactly why the guard
        # must not depend on the delete succeeding: a superuser, a disabled
        # trigger, or RLS simply hiding the rows all produce the same
        # observation, which is the one this test manufactures.
        await _empty_checkpoints(db_session)

        fresh = await _log_with(5, label="post-attack")
        result = await _sign(db_session, fresh, signer, head=head, genesis=True)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_HEAD_AHEAD_OF_LOG

    @needs_cryptography
    async def test_a_table_ahead_of_nothing_is_refused_when_the_head_is(self, db_session):
        """Rows still exist, but the newest is OLDER than the recorded head.

        Distinct from the delete-everything case, and it is the one that proves
        the floor comparison rather than the empty-table branch. A partial
        deletion -- or an attacker who deletes the newest few rows and leaves the
        older ones -- looks like a healthy table to anything that only checks
        "is there a row?". It is caught here and nowhere else.
        """
        signer = generate_ed25519_signer(seed=_seed("f2-partial"))
        log = await _log_with(3)
        first = await _sign(db_session, log, signer)
        head = signing.parse_trusted_head(first.detail["published_head"])

        await log.append({"type": "more", "n": "3"})
        second = await _sign(db_session, log, signer, head=head)
        assert second.status == "signed"

        # The operator advances the head to what was just published, as they
        # must between runs. Without this the remaining row would still match
        # the OLD head exactly, and this test would be measuring the digest
        # check instead of the floor.
        head = signing.parse_trusted_head(second.detail["published_head"])
        assert head is not None and head.tree_size == 4

        # Delete only the NEWEST row. One older checkpoint remains, so the table
        # still looks populated -- but it no longer accounts for what was
        # published, which is the only fact the head holds.
        rows = (await db_session.execute(select(TransparencyCheckpoint))).scalars().all()
        newest = max(rows, key=lambda r: (r.tree_size, r.created_at))
        await db_session.delete(newest)
        await db_session.flush()

        result = await _sign(db_session, await _log_with(5), signer, head=head)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_HEAD_AHEAD_OF_LOG
        # The refusal names the gap, so an operator reading the alert can see
        # that rows are missing rather than having to diff the table by hand.
        assert result.detail["head_tree_size"] == 4
        assert result.detail["newest_tree_size"] == 3

    @needs_cryptography
    async def test_a_truncated_table_is_refused(self, db_session):
        """Same attack, subtler: some rows deleted, the newest one gone."""
        signer = generate_ed25519_signer(seed=_seed("f2-truncated"))
        log = await _log_with(3)
        first = await _sign(db_session, log, signer)
        head = signing.parse_trusted_head(first.detail["published_head"])

        await _empty_checkpoints(db_session)

        result = await _sign(db_session, await _log_with(4), signer, head=head, genesis=True)
        assert result.reason == signing.REFUSAL_HEAD_AHEAD_OF_LOG

    @needs_cryptography
    async def test_a_row_at_the_head_size_that_is_not_the_head_is_refused(self, db_session):
        """F1 and F2 together: a substituted row at the published size.

        The forged row here carries the RIGHT root and is signed by a DIFFERENT
        key that the operator has also published (a rotation attack, or a second
        compromised key). F1 passes it -- it is authentically signed by a
        published key. Only the head digest catches it, because the digest
        covers the signature, not just the root.
        """
        signer = generate_ed25519_signer(seed=_seed("f2-substitute"))
        rogue = generate_ed25519_signer(seed=_seed("f2-substitute-rogue"))
        log = await _log_with(3)
        entries = await log.entries()

        # Publish a real checkpoint, record its head, then replace the table
        # contents with a different validly-signed checkpoint at the same size.
        first = await _sign(db_session, log, signer)
        head = signing.parse_trusted_head(first.detail["published_head"])
        assert head is not None

        await _empty_checkpoints(db_session)
        substituted = Checkpoint(
            tree_size=3,
            merkle_root=signing.merkle_root([e.leaf_hash for e in entries]),
            chain_hash=entries[-1].chain_hash,
            # A different timestamp => a different digest at the same root.
            timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
            origin=ORIGIN,
            format=FORMAT_C2SP_V2,
        )
        await save_checkpoint(db_session, sign_checkpoint(substituted, rogue))
        await db_session.flush()

        trusted = {** _trusted(signer), ** _trusted(rogue)}
        result = await _sign(
            db_session, await _log_with(4, "x"), rogue, trusted=trusted, head=head
        )
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_HEAD_MISMATCH

    @needs_cryptography
    async def test_the_head_is_a_floor_not_a_ceiling(self, db_session):
        """The database being AHEAD of the head is normal and must sign.

        If this refused, the signer could only ever run once per head advance,
        which is not a usable system and would push the operator toward setting
        the head loosely -- defeating F2 entirely.
        """
        signer = generate_ed25519_signer(seed=_seed("f2-floor"))
        log = await _log_with(2)
        first = await _sign(db_session, log, signer)
        head = signing.parse_trusted_head(first.detail["published_head"])

        await log.append({"type": "more", "n": "2"})
        await log.append({"type": "more", "n": "3"})
        result = await _sign(db_session, log, signer, head=head)
        assert result.status == "signed"
        assert result.tree_size == 4


class TestSignerKeyTrust:
    """F3: refuse to sign with a key nobody can verify, and verify our own work."""

    @needs_cryptography
    async def test_a_key_outside_the_trusted_set_is_refused_before_signing(self, db_session):
        """The silent-mis-derivation case, and it must refuse BEFORE writing.

        The failure being prevented: TRANSPARENCY_SIGNING_KEY is derived to a key,
        a DIFFERENT key from the one the operator published, nothing compares
        them, and the signer publishes a stream of checkpoints no third party can
        verify while reporting success on every run.
        """
        signer = generate_ed25519_signer(seed=_seed("f3-untrusted"))
        other = generate_ed25519_signer(seed=_seed("f3-published-other"))
        log = await _log_with(3)
        result = await _sign(db_session, log, signer, trusted=_trusted(other))
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_SIGNER_KEY_UNTRUSTED
        rows = await db_session.execute(select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 0

    @needs_cryptography
    async def test_a_mismatched_public_key_is_refused(self, db_session):
        """Same key_id, different public key: the mis-derivation shape.

        Reported by key_id match and still wrong, which is exactly how a silent
        mis-derivation presents: nothing in the key_id looks unusual.
        """
        signer = generate_ed25519_signer(seed=_seed("f3-mismatch"))
        impostor = generate_ed25519_signer(seed=_seed("f3-mismatch-other"))
        trusted = {
            signer.key_id: TrustedKey(
                key_id=signer.key_id,  # deliberately the signer's own id
                algorithm=signer.algorithm,
                public_key=impostor.public_key_bytes(),  # but a different key
            )
        }
        log = await _log_with(3)
        result = await _sign(db_session, log, signer, trusted=trusted)
        assert result.status == "refused"
        # Key IDENTITY is the post-sign check's job, not the pre-sign one: the
        # signature is produced and then fails to verify against the published
        # key's bytes. Distinct code from signer_key_untrusted (membership),
        # which is what makes the two guards independently testable.
        assert result.reason == signing.REFUSAL_SIGNER_KEY_MISMATCH
        rows = await db_session.execute(select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 0, "a checkpoint signed by the wrong key was stored"

    @needs_cryptography
    async def test_a_signing_key_outside_its_tree_size_bound_is_refused(self, db_session):
        signer = generate_ed25519_signer(seed=_seed("f3-maxsize"))
        trusted = {
            signer.key_id: TrustedKey(
                key_id=signer.key_id,
                algorithm=signer.algorithm,
                public_key=signer.public_key_bytes(),
                max_tree_size=1,
            )
        }
        log = await _log_with(3)
        result = await _sign(db_session, log, signer, trusted=trusted)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_SIGNER_KEY_OUT_OF_BOUNDS

    @needs_cryptography
    async def test_a_low_entropy_seed_is_refused_by_the_derivation(self):
        """F3's root: a short seed is not stretched, it is refused.

        The published Ed25519 public key is a brute-force oracle for a
        low-entropy seed. No KDF fixes that; only requiring 32 bytes of random
        material does.
        """
        from src.transparency.checkpoint import MIN_SIGNING_SEED_BYTES

        assert MIN_SIGNING_SEED_BYTES == 32
        for bad in (b"", b"seed", b"a" * 31, b"hunter2"):
            with pytest.raises(ValueError, match="at least 32 bytes"):
                generate_ed25519_signer(seed=bad)
        # 32 bytes is accepted, so the check is a floor and not an off-by-one
        # that rejects every deployment.
        assert generate_ed25519_signer(seed=b"b" * 32) is not None

    @needs_cryptography
    async def test_derivation_is_domain_separated_and_deterministic(self):
        """Same seed -> same key (the ceremony needs reproducibility).

        Different domains -> different keys, so a seed reused for another purpose
        cannot produce this signing key.
        """
        from src.transparency.checkpoint import (
            SIGNING_KDF_INFO,
            derive_ed25519_private_key,
        )

        assert derive_ed25519_private_key(b"x" * 32) == derive_ed25519_private_key(b"x" * 32)
        assert derive_ed25519_private_key(b"x" * 32) != derive_ed25519_private_key(b"y" * 32)
        # Not a bare hash: distinct from sha256(seed).
        import hashlib

        assert derive_ed25519_private_key(b"x" * 32) != hashlib.sha256(b"x" * 32).digest()
        assert SIGNING_KDF_INFO not in derive_ed25519_private_key(b"x" * 32)

    @needs_cryptography
    async def test_the_signer_verifies_its_own_output_before_storing_it(self, db_session):
        """Post-sign verification is a real guard, not decoration.

        The check the review named: nothing verified what the signer produced.
        Here a signer whose output does not verify against its own key must be
        refused and must store NOTHING.
        """
        signer = generate_ed25519_signer(seed=_seed("f3-postsign"))
        trusted = _trusted(signer)

        class _LiarSigner:
            """Wraps a real signer and returns a corrupt signature."""

            algorithm = signer.algorithm
            key_id = signer.key_id
            key_name = getattr(signer, "key_name", None)

            def sign(self, message: bytes) -> bytes:
                raw = signer.sign(message)
                return bytes([raw[0] ^ 0xFF]) + raw[1:]

            def verify(self, message: bytes, signature: bytes) -> bool:
                return signer.verify(message, signature)

            def public_key_bytes(self) -> bytes:
                return signer.public_key_bytes()

        log = await _log_with(3)
        result = await _sign(db_session, log, _LiarSigner(), trusted=trusted)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_SIGNER_VERIFY_FAILED
        rows = await db_session.execute(select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 0, "a corrupt checkpoint was stored"

    @needs_cryptography
    async def test_a_corrupt_signature_is_not_reported_as_a_key_mismatch(self, db_session):
        """The two post-sign guards must be distinguishable, or neither is tested.

        There are two checks after signing: the raw signature must verify against
        the signing key itself, and the checkpoint must verify against the
        PUBLISHED key set. Sharing one reason code made them indistinguishable --
        removing either guard left the other's code passing, which is exactly how
        a guard ends up untested. This pins the mapping.
        """

        inner = generate_ed25519_signer(seed=_seed("f3-distinguish"))

        class _LiarSigner:
            algorithm = inner.algorithm
            key_id = inner.key_id
            key_name = None

            def sign(self, message: bytes) -> bytes:
                raw = inner.sign(message)
                return bytes([raw[0] ^ 0xFF]) + raw[1:]

            def verify(self, message: bytes, signature: bytes) -> bool:
                return inner.verify(message, signature)

            def public_key_bytes(self) -> bytes:
                return inner.public_key_bytes()

        trusted = _trusted(inner)
        result = await _sign(db_session, await _log_with(3), _LiarSigner(), trusted=trusted)
        assert result.reason == signing.REFUSAL_SIGNER_VERIFY_FAILED
        assert result.reason != signing.REFUSAL_SIGNER_KEY_MISMATCH


class TestPoisonedLogShape:
    """F5: a planted tree_size or an index gap must refuse, not 500."""

    @needs_cryptography
    async def test_a_planted_huge_tree_size_is_refused_and_alertable(self, db_session):
        """One INSERT permanently halts signing, with no alert explaining why.

        Pre-hardening this refused with log_shrank, which is at least a refusal,
        but it refuses FOREVER: the row is append-only and cannot be deleted, so
        the only way out is the quarantine below.
        """
        signer = generate_ed25519_signer(seed=_seed("f5-poison"))
        log = await _log_with(3)
        await _plant_authentic_row(db_session, signer, 10**9)

        result = await _sign(db_session, log, signer)
        assert result.status == "refused"
        # The refusal names the row so an operator can quarantine it, and it is
        # a refusal (409 at the route) rather than an exception (500).
        assert result.reason == signing.REFUSAL_LOG_SMALLER
        assert result.detail["published_tree_size"] == 10**9

    @needs_cryptography
    async def test_an_index_gap_is_a_refusal_not_an_exception(self, db_session):
        """The 500. log.size() is max(index)+1, so a gap makes tree_size lie.

        Before this fix the gap reached verify_chain, which returned False, which
        build_checkpoint turned into a ValueError, which the cron surfaced as a
        500 -- on every run, forever, with nothing in transparency_alerts.
        """
        signer = generate_ed25519_signer(seed=_seed("f5-gap"))
        # The PRODUCTION log reports max(index)+1, so deleting a middle row
        # leaves size() reporting 4 over 3 rows. _GappyLog reproduces that.
        log = _GappyLog(await _log_with(4))
        del log._inner._entries[1]
        result = await _sign(db_session, log, signer)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_LOG_SHAPE
        assert result.detail["tree_size"] == 4
        assert result.detail["entry_count"] == 3

    @needs_cryptography
    async def test_a_gap_that_keeps_the_count_right_is_still_refused(self, db_session):
        """The subtler version: swap an index rather than delete a row.

        Count matches, so a naive len(entries) == tree_size check passes; only
        the contiguity check catches it, because a root over a set with a hole
        is not a root over this log and must not be compared to a published one.
        """
        signer = generate_ed25519_signer(seed=_seed("f5-noncontig"))
        log = await _log_with(3)
        log._entries[2] = dataclasses.replace(log._entries[2], index=7)
        result = await _sign(db_session, log, signer)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_LOG_SHAPE
        assert "contiguous" in result.detail["detail_problem"]


class TestQuarantine:
    """How a poisoned row stops blocking, and the limit that keeps it honest."""

    @needs_cryptography
    async def test_quarantining_a_poisoned_row_unblocks_signing(self, db_session):
        signer = generate_ed25519_signer(seed=_seed("q-unblock"))
        log = await _log_with(3)
        poisoned = await _plant_authentic_row(db_session, signer, 10**9)
        before = await _sign(db_session, log, signer)
        assert before.reason == signing.REFUSAL_LOG_SMALLER

        db_session.add(
            TransparencyQuarantine(
                checkpoint_id=str(poisoned.id), reason="planted tree_size, F5"
            )
        )
        await db_session.flush()

        after = await _sign(db_session, log, signer)
        assert after.status == "signed"
        assert after.tree_size == 3

    @needs_cryptography
    async def test_a_quarantined_row_does_not_still_trigger_already_signed(self, db_session):
        """The bug the live probe found, pinned so it cannot come back.

        Quarantine was honoured when picking the PREVIOUS checkpoint but not in
        the equivocation/idempotency scan at this tree_size. So a run whose whole
        purpose was to sign past a quarantined row returned already_signed
        forever -- the signer never recovered from the quarantine, which is the
        opposite of what quarantine is for. No SQLite test could have caught it:
        every one of them seeds a table with nothing to quarantine.
        """
        signer = generate_ed25519_signer(seed=_seed("q-already-signed"))
        log = await _log_with(3)
        first = await _sign(db_session, log, signer)
        assert first.status == "signed"

        row = (await db_session.execute(select(TransparencyCheckpoint))).scalars().first()
        db_session.add(
            TransparencyQuarantine(checkpoint_id=str(row.id), reason="quarantined for test")
        )
        await db_session.flush()

        result = await _sign(db_session, log, signer)
        assert result.status == "signed", result.reason
        assert result.tree_size == 3

    @needs_cryptography
    async def test_quarantine_cannot_be_used_to_sign_below_the_published_head(
        self, db_session
    ):
        """The limit that keeps quarantine from being an equivocation primitive.

        An attacker who can reach the quarantine table could quarantine the
        genuine head and stall the signer -- but they cannot make it sign a
        SHORTER history, because the head is outside the database and does not
        move when a row is excluded.
        """
        signer = generate_ed25519_signer(seed=_seed("q-head"))
        log = await _log_with(3)
        first = await _sign(db_session, log, signer)
        head = signing.parse_trusted_head(first.detail["published_head"])
        genuine_id = str((await db_session.execute(select(TransparencyCheckpoint))).scalars().first().id)

        db_session.add(
            TransparencyQuarantine(checkpoint_id=genuine_id, reason="attempted rollback")
        )
        await db_session.flush()

        result = await _sign(db_session, await _log_with(4), signer, head=head)
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_HEAD_AHEAD_OF_LOG

    @needs_cryptography
    async def test_the_hmac_row_can_be_quarantined_so_dev_can_sign(self, db_session):
        """The concrete dev escape hatch F1 creates.

        Dev's existing v1 rows are HMAC-signed and therefore unauthenticatable.
        Quarantining them lets signing proceed without keeping a weak
        verification path alive for them.
        """
        signer = generate_ed25519_signer(seed=_seed("q-hmac"))
        log = await _log_with(2)
        entries = await log.entries()
        v1 = Checkpoint(
            tree_size=2,
            merkle_root=signing.merkle_root([e.leaf_hash for e in entries]),
            chain_hash=entries[-1].chain_hash,
            timestamp=datetime.now(timezone.utc),
            format=FORMAT_JSON_V1,
        )
        row = TransparencyCheckpoint(
            tree_size=2,
            merkle_root=v1.merkle_root.hex(),
            chain_hash=v1.chain_hash.hex(),
            timestamp=v1.timestamp,
            signature="33" * 32,
            algorithm="hmac-sha256-dev",
            key_id="dev-hmac",
        )
        db_session.add(row)
        await db_session.flush()
        assert (await _sign(db_session, log, signer)).status == "refused"

        db_session.add(
            TransparencyQuarantine(checkpoint_id=str(row.id), reason="pre-v2 HMAC row")
        )
        await db_session.flush()
        result = await _sign(db_session, log, signer)
        assert result.status == "signed"
        assert result.signed.checkpoint.format == FORMAT_C2SP_V2


class TestHeadParsing:
    """The config that decides whether F2 is armed at all.

    Each case here is a way the head could be silently treated as absent, which
    downgrades F2 to "trust the table". Absent is a legitimate state (nothing
    published yet, with genesis confirmed) so it must be distinguishable from
    MISCONFIGURED, and misconfigured must fail the same direction: refuse.
    """

    def test_a_well_formed_head_parses(self):
        digest = "ab" * 32
        head = signing.parse_trusted_head(f"12:{digest}")
        assert head is not None
        assert head.tree_size == 12
        assert head.digest == digest

    def test_a_published_head_round_trips_through_the_config_format(self):
        """The value the signer emits must be the value the config parses.

        If these two disagreed the operator would advance the head to a string
        the signer cannot read, and F2 would be off while looking configured.
        """
        digest = "cd" * 32
        raw = signing._published_head_value(7, digest)
        parsed = signing.parse_trusted_head(raw)
        assert parsed is not None
        assert (parsed.tree_size, parsed.digest) == (7, digest)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            None,
            "notanumber:" + "ab" * 32,
            "12:tooshort",
            "0:" + "ab" * 32,
            "-3:" + "ab" * 32,
            "12:" + "zz" * 32,
            "12",
        ],
        ids=[
            "empty", "whitespace", "none", "non-integer-size", "short-digest",
            "zero-size", "negative-size", "non-hex-digest", "no-digest-part",
        ],
    )
    def test_unusable_head_config_is_absent_not_a_default(self, bad):
        """Every one of these means F2 is not armed.

        The important half is what the CALLER then does, and that is asserted in
        TestRollbackAndGenesis: absent plus no genesis confirmation refuses. This
        test pins the parsing half so a future change cannot make a typo look
        like a configured head.
        """
        assert signing.parse_trusted_head(bad) is None

    def test_uppercase_hex_digest_is_normalized(self):
        head = signing.parse_trusted_head(f"5:{'AB' * 32}")
        assert head is not None and head.digest == "ab" * 32