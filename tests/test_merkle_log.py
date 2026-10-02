"""Tests for the signed Merkle transparency log.

Covers the six properties the layer exists to provide: a verifiable chain, chain
verification that a single tampered entry breaks, inclusion proofs that verify,
proofs that fail against the wrong checkpoint, checkpoint signatures that only
check out for the right key, and an anchor stub that records what it was handed.
"""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from src.transparency.anchoring import OTS_CALENDAR_URL, AnchorProvider, OpenTimestampsStubProvider
from src.transparency.checkpoint import (
    Checkpoint,
    HmacDevSigner,
    Signer,
    build_checkpoint,
    checkpoint_digest,
    ed25519_available,
    generate_ed25519_signer,
    merkle_root,
    sign_checkpoint,
    verify_checkpoint,
)
from src.transparency.log import (
    GENESIS_CHAIN_HASH,
    InMemoryMerkleLog,
    LogEntry,
    MerkleLog,
    build_entry,
    canonical_json,
    leaf_hash,
    verify_chain,
)
from src.transparency.proofs import inclusion_proof, proof_root, verify_inclusion

SECRET = b"test-only-secret"
OTHER_SECRET = b"a-different-test-secret"


async def _filled_log(count: int) -> InMemoryMerkleLog:
    log = InMemoryMerkleLog()
    for index in range(count):
        await log.append({"url": f"https://example.test/{index}", "status": 200})
    return log


async def _signed_checkpoint(log: MerkleLog, tree_size: int, signer: Signer):
    return sign_checkpoint(await build_checkpoint(log, tree_size), signer)


class TestChain:
    async def test_appending_n_entries_produces_a_verifiable_chain(self):
        """Indices are gapless, entry 0 chains from genesis, and every link holds."""
        log = await _filled_log(12)
        entries = await log.entries()

        assert await log.size() == 12
        assert [entry.index for entry in entries] == list(range(12))
        assert entries[0].chain_hash != GENESIS_CHAIN_HASH
        assert verify_chain(entries)

        # Genesis chaining is explicit, not incidental.
        assert entries[0].chain_hash == build_entry(
            0, entries[0].payload, timestamp=entries[0].timestamp
        ).chain_hash

    async def test_chain_matches_a_hand_computed_digest(self):
        """The chain is exactly sha256(prev || leaf) over canonical payload bytes."""
        import hashlib

        payload = {"b": 2, "a": 1}
        expected_canonical = b'{"a":1,"b":2}'
        assert canonical_json(payload) == expected_canonical
        expected_leaf = leaf_hash(payload)

        entry = build_entry(0, payload, timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc))
        assert entry.leaf_hash == expected_leaf
        assert entry.canonical_payload == expected_canonical

        following = build_entry(1, {"n": 1}, previous_chain_hash=entry.chain_hash)
        assert following.chain_hash == hashlib.sha256(
            entry.chain_hash + leaf_hash({"n": 1})
        ).digest()
        assert following.chain_hash != entry.chain_hash

    async def test_tampering_with_one_stored_entry_breaks_chain_verification(self):
        """A rewritten payload must invalidate the leaf hash and every link after it."""
        log = await _filled_log(10)
        entries = list(await log.entries())
        assert verify_chain(entries)

        tampered_payload = {**entries[4].payload, "status": 404}
        entries[4] = dataclasses.replace(
            entries[4],
            payload=tampered_payload,
            canonical_payload=canonical_json(tampered_payload),
        )
        assert not verify_chain(entries)

        # Editing an entry's hashes by hand does not help: the link is recomputed.
        entries[4] = dataclasses.replace(entries[4], chain_hash=bytes(32))
        assert not verify_chain(entries)

    async def test_dropping_an_entry_breaks_the_chain(self):
        """A deleted entry shifts every later index, so the chain no longer lines up."""
        log = await _filled_log(8)
        entries = list(await log.entries())
        assert not verify_chain(entries[:4] + entries[5:])

    async def test_entries_are_frozen(self):
        log = await _filled_log(1)
        entry = (await log.entries())[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.leaf_hash = bytes(32)


class TestInclusionProofs:
    async def test_valid_inclusion_proof_verifies(self):
        log = await _filled_log(9)
        signer = HmacDevSigner(SECRET)
        signed = await _signed_checkpoint(log, 9, signer)

        for index in (0, 4, 8):
            entry = await log.get(index)
            proof = await inclusion_proof(log, index, signed.checkpoint.tree_size)
            assert verify_inclusion(entry, proof, signed)

    async def test_proof_folds_to_the_checkpoint_root(self):
        """proof_root is the whole mechanism: it must land on merkle_root."""
        log = await _filled_log(7)
        signed = await _signed_checkpoint(log, 7, HmacDevSigner(SECRET))
        entry = await log.get(3)
        proof = await inclusion_proof(log, 3, 7)
        assert proof_root(entry.leaf_hash, proof.index, proof.siblings) == signed.checkpoint.merkle_root

    async def test_proof_against_a_wrong_checkpoint_fails(self):
        log = await _filled_log(10)
        signer = HmacDevSigner(SECRET)
        small = await _signed_checkpoint(log, 4, signer)
        large = await _signed_checkpoint(log, 10, signer)

        entry = await log.get(1)
        proof = await inclusion_proof(log, 1, large.checkpoint.tree_size)

        # Right entry, right proof, wrong tree_size on the proof.
        assert not verify_inclusion(entry, proof, small)
        # And an entry from a different history cannot borrow a valid-looking proof.
        rewritten = InMemoryMerkleLog()
        for index in range(10):
            await rewritten.append({"url": f"https://example.test/{index}", "status": 404})
        rewritten_entry = await rewritten.get(1)
        assert not verify_inclusion(rewritten_entry, proof, large)

    async def test_tampered_entry_fails_its_own_valid_proof(self):
        log = await _filled_log(6)
        signed = await _signed_checkpoint(log, 6, HmacDevSigner(SECRET))
        entry = await log.get(2)
        proof = await inclusion_proof(log, 2, 6)

        forged = dataclasses.replace(
            entry,
            payload={**entry.payload, "status": 500},
        )
        assert not verify_inclusion(forged, proof, signed)

    async def test_proof_outside_the_checkpoint_is_rejected(self):
        log = await _filled_log(5)
        with pytest.raises(ValueError):
            await inclusion_proof(log, 5, 5)


class TestCheckpointSignatures:
    async def test_signature_verifies_with_the_right_key(self):
        log = await _filled_log(5)
        signer = HmacDevSigner(SECRET)
        signed = await _signed_checkpoint(log, 5, signer)

        assert verify_checkpoint(signed, signer)
        # A fresh object over the same bytes round-trips through JSON unchanged.
        assert verify_checkpoint(type(signed).from_dict(signed.to_dict()), signer)

    async def test_signature_fails_with_the_wrong_key(self):
        log = await _filled_log(5)
        signed = await _signed_checkpoint(log, 5, HmacDevSigner(SECRET))
        impostor = HmacDevSigner(OTHER_SECRET, key_id="test-hmac")  # Same secret length, different key

        assert not verify_checkpoint(signed, impostor)

        # A tampered checkpoint body must not verify under its own signature.
        forged = dataclasses.replace(signed, checkpoint=dataclasses.replace(
            signed.checkpoint, merkle_root=bytes(32)
        ))
        assert not verify_checkpoint(forged, HmacDevSigner(SECRET))

    @pytest.mark.skipif(not ed25519_available(), reason="cryptography is not installed")
    async def test_ed25519_signature_verifies_only_with_its_public_key(self):
        log = await _filled_log(4)
        signer = generate_ed25519_signer(seed=b"test-seed")
        signed = await _signed_checkpoint(log, 4, signer)

        from src.transparency.checkpoint import Ed25519Verifier

        verifier = Ed25519Verifier(signer.public_key_bytes(), key_id=signer.key_id)
        assert verify_checkpoint(signed, verifier)
        assert not verify_checkpoint(signed, generate_ed25519_signer(seed=b"other-seed"))


class TestCheckpointContents:
    async def test_checkpoint_commits_to_tree_size_root_and_chain_head(self):
        log = await _filled_log(6)
        checkpoint = await build_checkpoint(log, 4)

        assert checkpoint.tree_size == 4
        assert checkpoint.merkle_root == merkle_root((await log.leaf_hashes())[:4])
        assert checkpoint.chain_hash == (await log.get(3)).chain_hash
        assert checkpoint.timestamp.tzinfo is not None

    async def test_checkpoint_larger_than_the_log_is_refused(self):
        log = await _filled_log(3)
        with pytest.raises(ValueError):
            await build_checkpoint(log, 4)

    async def test_checkpoint_over_a_broken_chain_is_refused(self):
        """S-P1-3: build_checkpoint verifies the chain before signing. A log
        whose entries were rewritten behind the log's back must not be
        laundered into a 'signed' checkpoint."""

        class TamperedLog(InMemoryMerkleLog):
            async def entries(self):
                entries = list(await super().entries())
                tampered = dict(entries[1].payload)
                tampered["url"] = "https://evil.test/rewritten"
                entries[1] = dataclasses.replace(entries[1], payload=tampered)
                return tuple(entries)

        log = TamperedLog()
        for index in range(3):
            await InMemoryMerkleLog.append(log, {"url": f"https://example.test/{index}"})
        with pytest.raises(ValueError, match="refusing to checkpoint"):
            await build_checkpoint(log, 3)

    async def test_anchor_digest_covers_the_signature(self):
        log = await _filled_log(4)
        signed = await _signed_checkpoint(log, 4, HmacDevSigner(SECRET))
        assert len(checkpoint_digest(signed)) == 64
        assert checkpoint_digest(signed) != checkpoint_digest(
            await _signed_checkpoint(log, 4, HmacDevSigner(OTHER_SECRET))
        )


class TestAnchoring:
    async def test_stub_records_what_it_was_given(self):
        provider = OpenTimestampsStubProvider()
        assert isinstance(provider, AnchorProvider)

        log = await _filled_log(4)
        signed = await _signed_checkpoint(log, 4, HmacDevSigner(SECRET))
        digest = checkpoint_digest(signed)

        attestation = await provider.anchor(digest)
        assert provider.submitted == [digest]
        assert attestation.provider == "opentimestamps-stub"
        assert attestation.digest_hex == digest
        assert attestation.timestamp.tzinfo is not None
        assert OTS_CALENDAR_URL.encode() in attestation.receipt
        assert b'"stub":true' in attestation.receipt

    async def test_stub_records_every_digest_in_order(self):
        provider = OpenTimestampsStubProvider()
        digests = ["a" * 64, "b" * 64]
        for digest in digests:
            await provider.anchor(digest)
        assert provider.submitted == digests

    async def test_stub_rejects_a_non_digest(self):
        with pytest.raises(ValueError):
            await OpenTimestampsStubProvider().anchor("not-a-digest")


class TestInterface:
    async def test_in_memory_log_satisfies_the_protocol(self):
        log = InMemoryMerkleLog()
        assert isinstance(log, MerkleLog)
        assert await log.head() == GENESIS_CHAIN_HASH
        assert await log.get(0) is None

        entry: LogEntry = await log.append({"n": 1})
        assert entry.index == 0
        assert await log.get(0) == entry

    async def test_sqlalchemy_backed_log_appends_and_verifies(self):
        """Same interface, real table. Proves the ORM model matches the hashing."""
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from src.transparency.log import MerkleLogEntry, SqlAlchemyMerkleLog, TransparencyBase

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(TransparencyBase.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                log = SqlAlchemyMerkleLog(session)
                assert isinstance(log, MerkleLog)

                for index in range(6):
                    await log.append({"url": f"https://example.test/{index}"})

                assert await log.size() == 6
                assert verify_chain(await log.entries())
                assert await log.head() == (await log.get(5)).chain_hash

                entry = await log.get(2)
                signed = await _signed_checkpoint(log, 6, HmacDevSigner(SECRET))
                proof = await inclusion_proof(log, 2, 6)
                assert verify_inclusion(entry, proof, signed)

                rows = await session.execute(
                    select(func.count()).select_from(MerkleLogEntry)
                )
                assert rows.scalar_one() == 6
        finally:
            await engine.dispose()

    async def test_checkpoint_over_an_empty_log_uses_the_genesis_chain_hash(self):
        log = InMemoryMerkleLog()
        checkpoint = await build_checkpoint(log, 0)
        assert checkpoint.tree_size == 0
        assert checkpoint.chain_hash == GENESIS_CHAIN_HASH

    def test_hmac_dev_signer_is_marked_not_for_production(self):
        """The fallback's status must be visible in code, not just in the docs."""
        assert "NOT FOR PRODUCTION" in (HmacDevSigner.__doc__ or "")
        assert HmacDevSigner.algorithm == "hmac-sha256-dev"
        assert issubclass(HmacDevSigner, object)
        assert isinstance(HmacDevSigner(b"x"), Signer)

    def test_signers_expose_algorithm_and_key_id(self):
        signer = HmacDevSigner(SECRET, key_id="k1")
        assert (signer.algorithm, signer.key_id) == ("hmac-sha256-dev", "k1")
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        checkpoint = Checkpoint(
            tree_size=1,
            merkle_root=bytes(32),
            chain_hash=GENESIS_CHAIN_HASH,
            timestamp=base,
        )
        # Domain separated, and every field of the body is covered.
        assert checkpoint.signing_bytes().startswith(b"n1:merkle-checkpoint:v1")
        assert checkpoint.signing_bytes() != dataclasses.replace(
            checkpoint, timestamp=base + timedelta(minutes=1)
        ).signing_bytes()
        assert checkpoint.signing_bytes() != dataclasses.replace(
            checkpoint, tree_size=2
        ).signing_bytes()