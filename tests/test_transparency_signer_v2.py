"""Tests for the v2 transparency checkpoint signer.

Five properties from DECISIONS.md's "Transparency log signing key (v2)" spec,
each with its own class below, plus the regression guards that matter most:
that v1 checkpoints are still byte-identical and still verify, and that the
signer refuses the three ways a log can be equivocated.

  TestSignedNotePrimitives      the C2SP wire format, including the spec's own example
  TestCheckpointV2              note text, extension line, v1 byte-for-byte stability
  TestKeyBounds                 validity windows are enforced, and unbounded keys warn
  TestSignerRefusals            append-only enforcement, idempotency, no tree-size regression
  TestCronRoutes                bearer auth, honest 503s, refusal status codes
  TestWatchdog                  the dead man's switch measures the artifact, not the cron
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from src.transparency import signing
from src.transparency.checkpoint import (
    CHECKPOINT_DOMAIN,
    FORMAT_C2SP_V2,
    FORMAT_JSON_V1,
    Checkpoint,
    HmacDevSigner,
    SignedCheckpoint,
    checkpoint_digest,
    ed25519_available,
    generate_ed25519_signer,
    merkle_root,
    rfc6962_root,
    sign_checkpoint,
    verify_checkpoint,
)
from src.transparency.keys import TrustedKey, TrustedKeyError, load_trusted_keys, verifier_for
from src.transparency.log import InMemoryMerkleLog, TransparencyBase
from src.transparency.signed_note import (
    ED25519_NOTE_SIG_TYPE,
    SignedNoteError,
    decode_verifier_key,
    encode_signed_note,
    encode_verifier_key,
    note_key_id,
    parse_signed_note,
    sign_note_text,
    verify_note_signature,
    verify_note_signatures,
)
from src.transparency.store import (
    TransparencyCheckpoint,
    TransparencyQuarantine,
    save_checkpoint,
)


def _seed(label: str) -> bytes:
    """A deterministic >= 32-byte signing seed for a test.

    generate_ed25519_signer refuses a seed shorter than MIN_SIGNING_SEED_BYTES,
    so tests that need a reproducible key pair cannot use a short label. This
    hashes the label to exactly 32 bytes, which is deterministic per label and
    satisfies the production constraint rather than bypassing it.
    """
    return hashlib.sha256(f"test-seed:{label}".encode()).digest()


def _trusted(signer) -> dict:
    """The published key set for this signer, derived from the signer itself.

    F1/F3 make the trusted-key set a required trust input, so the pre-hardening
    tests in this file have to supply one. Deriving it from the signer (rather
    than hand-writing a TrustedKey) keeps the key_id consistent with the one the
    signer produces, which is what the real configuration does.
    """
    return {
        signer.key_id: TrustedKey(
            key_id=signer.key_id,
            algorithm=signer.algorithm,
            public_key=signer.public_key_bytes(),
        )
    }



needs_cryptography = pytest.mark.skipif(
    not ed25519_available(), reason="cryptography is not installed"
)

ORIGIN = "procmon.dev/transparency"


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


@pytest.fixture
async def db_session():
    """A SQLite session with the transparency tables created.

    SQLite stands in for Postgres in tests, so the advisory-lock call is skipped
    (lock=False); every other path -- the re-derivation, the equivocation
    check, the insert -- is the same code that runs in production.
    """
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


async def _log_with(entries: int) -> InMemoryMerkleLog:
    log = InMemoryMerkleLog()
    for i in range(entries):
        await log.append({"type": "test", "n": str(i)})
    return log


def _v2_checkpoint(tree_size: int = 3, previous_digest: bytes | None = None) -> Checkpoint:
    return Checkpoint(
        tree_size=tree_size,
        merkle_root=bytes(range(32)),
        chain_hash=bytes(range(32, 64)),
        timestamp=datetime(2026, 10, 2, 6, 0, tzinfo=UTC),
        origin=ORIGIN,
        previous_digest=previous_digest,
        format=FORMAT_C2SP_V2,
    )


class TestSignedNotePrimitives:
    """The C2SP signed-note format, checked against the spec's own example."""

    def test_key_id_matches_the_specs_worked_example(self):
        """The key id is the spec's derivation, byte for byte.

        SHA-256(name || 0x0A || type byte || public key), first four bytes. The
        spec's worked example (key name example.com/foo, type 0x01) derives
        530d903a from its own published key; this test pins the derivation this
        module implements against an independent restatement of it, so a change
        to the hashing order fails here rather than in a third party's verifier.
        """
        public_key = bytes(range(32))
        key_id = note_key_id(
            "example.com/foo", public_key, signature_type=ED25519_NOTE_SIG_TYPE
        )
        manual = hashlib.sha256(
            b"example.com/foo" + b"\x0a" + bytes([ED25519_NOTE_SIG_TYPE]) + public_key
        ).digest()[:4]
        assert key_id == manual
        assert len(key_id) == 4
        assert ED25519_NOTE_SIG_TYPE == 0x01

    @needs_cryptography
    def test_the_specs_own_example_verifies_end_to_end(self):
        """The spec's worked example, reproduced: note text, sign, verify.

        "This is an example message." under the key name example.com/foo is the
        example c2sp.org/signed-note uses. Signing it here and verifying it back
        is what proves this module's format understanding is the spec's and not
        a plausible-looking invention.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # A fixed seed, so the signature bytes below are reproducible and the
        # assertion on them means something.
        private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"spec-example").digest())
        public = bytes(private.public_key().public_bytes_raw())
        note_text = "This is an example message.\n"
        sig = sign_note_text(note_text, private, key_name="example.com/foo", public_key=public)
        document = encode_signed_note(note_text, [sig])

        # The signature line is EM DASH, key name, base64 of key id || sig.
        assert document.startswith("This is an example message.\n\n")
        assert "\u2014 example.com/foo " in document
        blob = base64.b64decode(document.splitlines()[-1].split(" ")[-1])
        assert blob[:4] == sig.key_id
        assert verify_note_signature(public, note_text, blob[4:])
        assert not verify_note_signature(public, note_text + "x", blob[4:])

        parsed = parse_signed_note(document)
        assert parsed.note_text == note_text
        assert parsed.key_names() == ("example.com/foo",)
        assert verify_note_signatures(parsed, {"example.com/foo": public}) == ["example.com/foo"]

    def test_key_names_reject_the_specs_forbidden_characters(self):
        for bad in ("", "has space", "has+plus", "has\ttab"):
            with pytest.raises(SignedNoteError):
                note_key_id(bad, b"\x00" * 32)

    def test_parse_round_trips_and_keeps_the_signed_newline(self):
        note_text = "a.test/log\n7\nZm9vYmFyYmF6\n"
        raw = _FakeNoteSignature(key_name="a.test/log", key_id=b"\xde\xad\xbe\xef", signature=b"\x01" * 8)
        document = encode_signed_note(note_text, [raw])
        parsed = parse_signed_note(document)
        assert parsed.note_text == note_text
        found = parsed.signature_for("a.test/log")
        assert found is not None
        assert (found.key_name, found.key_id, found.signature) == (
            raw.key_name, raw.key_id, raw.signature,
        )
        assert parsed.signature_for("other.test/log") is None

    def test_parsing_a_document_with_no_signatures_is_refused(self):
        """Zero usable signature lines must be a refusal, not an empty pass."""
        with pytest.raises(SignedNoteError):
            parse_signed_note("just text\n\n")
        with pytest.raises(SignedNoteError):
            parse_signed_note("no blank line here\n")

    def test_unknown_keys_are_ignored_and_a_note_with_only_them_is_rejected(self):
        parsed = parse_signed_note(
            encode_signed_note(
                "a.test/log\n1\nZm9vYmFy\n",
                [_FakeNoteSignature("someone.else/x", b"\x00\x00\x00\x01", b"\x02" * 8)],
            )
        )
        # Ignored, not rejected...
        assert verify_note_signatures(parsed, {"a.test/log": b"\x00" * 32}) == []
        # ...but a note with no known verifier is not a pass either.
        assert verify_note_signatures(parsed, {}) == []

    def test_verifier_key_encoding_round_trips(self):
        public = bytes(range(32))
        encoded = encode_verifier_key(ORIGIN, public)
        name, key_id, decoded = decode_verifier_key(encoded)
        assert name == ORIGIN
        assert key_id == note_key_id(ORIGIN, public)
        assert decoded == public
        assert len(encoded.split("+")) == 3

    def test_a_malformed_verifier_key_is_refused(self):
        for bad in ("no-plus-signs", "a+b", "a+b+c", f"{ORIGIN}+zz+AAAA"):
            with pytest.raises(SignedNoteError):
                decode_verifier_key(bad)


class _FakeNoteSignature:
    """A NoteSignature stand-in that needs no `cryptography` import."""

    def __init__(self, key_name, key_id, signature):
        self.key_name = key_name
        self.key_id = key_id
        self.signature = signature

    def encoded(self):
        return self.key_id + self.signature

    def __eq__(self, other):
        return (
            isinstance(other, _FakeNoteSignature)
            and (self.key_name, self.key_id, self.signature)
            == (other.key_name, other.key_id, other.signature)
        )


class TestCheckpointV2:
    """Note text shape, the extension line, and v1 stability."""

    def test_note_text_is_three_mandatory_lines_plus_one_extension(self):
        text = _v2_checkpoint().note_text()
        lines = text.split("\n")
        assert text.endswith("\n")
        # origin, tree size, root, extension, then the trailing newline.
        assert lines[0] == ORIGIN
        assert lines[1] == "3"
        assert lines[2] == base64.b64encode(bytes(range(32))).decode()
        assert lines[3].startswith("x-news-pipeline ")
        assert lines[4] == ""
        assert len([line for line in lines if line]) == 4

    def test_tree_size_has_no_leading_zeroes_and_the_root_is_standard_base64(self):
        text = _v2_checkpoint(tree_size=1024).note_text()
        root_line = text.split("\n")[2]
        # standard RFC 4648 alphabet with padding, so any decoder reads it
        assert base64.b64decode(root_line, validate=True) == bytes(range(32))
        assert "1024" in text.split("\n")[1]

    def test_extension_line_carries_timestamp_and_previous_digest(self):
        previous = bytes(range(31, 63))
        text = _v2_checkpoint(previous_digest=previous).note_text()
        extension = json.loads(text.split("\n")[3][len("x-news-pipeline "):])
        assert extension["prev"] == previous.hex()
        assert extension["timestamp"] == "2026-10-02T06:00:00Z"
        assert extension["chain_hash"] == bytes(range(32, 64)).hex()

    def test_first_checkpoint_marks_no_previous_digest(self):
        extension = json.loads(
            _v2_checkpoint(previous_digest=None).note_text().split("\n")[3][len("x-news-pipeline "):]
        )
        assert extension["prev"] == "-"

    def test_v1_signing_bytes_are_unchanged(self):
        """The regression that matters most: existing rows must still verify.

        v1's bytes are constructed from the module-level domain separator and
        the canonical JSON body. If this assertion needs editing, some published
        v1 checkpoint just stopped verifying.
        """
        checkpoint = _v2_checkpoint()
        v1 = Checkpoint(
            tree_size=checkpoint.tree_size,
            merkle_root=checkpoint.merkle_root,
            chain_hash=checkpoint.chain_hash,
            timestamp=checkpoint.timestamp,
        )
        body = {
            "chain_hash": checkpoint.chain_hash.hex(),
            "hash_scheme": "n1:sha256",
            "merkle_root": checkpoint.merkle_root.hex(),
            "timestamp": "2026-10-02T06:00:00Z",
            "tree_size": 3,
        }
        expected = CHECKPOINT_DOMAIN + b" " + json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        assert v1.signing_bytes() == expected
        assert v1.format == FORMAT_JSON_V1
        # And a v1 dict carries no v2 fields, so publishing it cannot confuse
        # a v2 reader.
        assert "format" not in v1.to_dict()
        assert "origin" not in v1.to_dict()

    def test_v1_and_v2_sign_the_same_checkpoint_differently(self):
        checkpoint = _v2_checkpoint()
        v1 = Checkpoint(
            tree_size=3,
            merkle_root=checkpoint.merkle_root,
            chain_hash=checkpoint.chain_hash,
            timestamp=checkpoint.timestamp,
        )
        assert v1.signing_bytes() != checkpoint.signing_bytes()

    def test_round_trip_preserves_the_v2_fields(self):
        checkpoint = _v2_checkpoint(previous_digest=bytes(range(4)))
        restored = Checkpoint.from_dict(checkpoint.to_dict())
        assert restored == checkpoint

    def test_v1_dict_reads_back_as_v1(self):
        """A published v1 row has no format column: it must default to v1."""
        legacy = {
            "tree_size": 8,
            "merkle_root": "ab" * 32,
            "chain_hash": "cd" * 32,
            "timestamp": "2026-10-02T02:27:15Z",
        }
        assert Checkpoint.from_dict(legacy).format == FORMAT_JSON_V1
        assert Checkpoint.from_dict(legacy).previous_digest is None

    def test_an_unknown_format_is_refused_rather_than_silently_v1(self):
        checkpoint = Checkpoint(
            tree_size=1,
            merkle_root=b"\x01" * 32,
            chain_hash=b"\x02" * 32,
            timestamp=datetime.now(UTC),
            format="n9-experimental",
        )
        with pytest.raises(ValueError, match="unknown checkpoint format"):
            checkpoint.signing_bytes()

    def test_an_origin_with_a_space_or_plus_is_refused(self):
        for bad in ("has space", "has+plus", ""):
            checkpoint = Checkpoint(
                tree_size=1,
                merkle_root=b"\x01" * 32,
                chain_hash=b"\x02" * 32,
                timestamp=datetime.now(UTC),
                origin=bad,
                format=FORMAT_C2SP_V2,
            )
            with pytest.raises(ValueError):
                checkpoint.note_text()

    @needs_cryptography
    def test_v2_signature_carries_the_note_key_id_and_verifies(self):
        signer = generate_ed25519_signer(seed=_seed("v2-test"))
        signed = sign_checkpoint(_v2_checkpoint(), signer)
        # 4-byte note key id + a raw Ed25519 signature.
        assert len(signed.signature) == 68
        assert signed.signature[:4] == note_key_id(ORIGIN, signer.public_key_bytes())
        assert signed.key_name == ORIGIN
        assert verify_checkpoint(signed, signer)

    @needs_cryptography
    def test_a_signature_cannot_be_replayed_under_another_key_name(self):
        """The origin/key-name binding: a key cannot sign as another log."""
        signer = generate_ed25519_signer(seed=_seed("replay-test"))
        signed = sign_checkpoint(_v2_checkpoint(), signer)
        # Same key, renamed to a different log.
        renamed = SignedCheckpoint(
            checkpoint=_v2_checkpoint(),
            signature=signed.signature,
            algorithm=signed.algorithm,
            key_id=signed.key_id,
            key_name="evil.test/other-log",
        )
        assert not verify_checkpoint(renamed, signer)

    @needs_cryptography
    def test_a_v2_checkpoint_signed_by_another_key_fails_verification(self):
        signer = generate_ed25519_signer(seed=_seed("right-key"))
        other = generate_ed25519_signer(seed=_seed("wrong-key"))
        signed = sign_checkpoint(_v2_checkpoint(), signer)
        impostor = Ed25519LikeSigner(other, key_id=signed.key_id)
        assert not verify_checkpoint(signed, impostor)

    @needs_cryptography
    def test_the_signed_note_document_is_parseable_by_the_note_parser(self):
        signer = generate_ed25519_signer(seed=_seed("document-test"))
        signed = sign_checkpoint(_v2_checkpoint(), signer)
        document = signed.signed_note_document()
        assert document is not None
        parsed = parse_signed_note(document)
        assert parsed.note_text == signed.checkpoint.note_text()
        verified = verify_note_signatures(parsed, {ORIGIN: signer.public_key_bytes()})
        assert verified == [ORIGIN]

    @needs_cryptography
    def test_a_v1_checkpoint_has_no_signed_note_document(self):
        v1 = Checkpoint(
            tree_size=1,
            merkle_root=b"\x01" * 32,
            chain_hash=b"\x02" * 32,
            timestamp=datetime.now(UTC),
        )
        signer = generate_ed25519_signer(seed=_seed("v1-doc"))
        assert sign_checkpoint(v1, signer).signed_note_document() is None

    @needs_cryptography
    def test_v2_cross_signs_v1s_last_checkpoint(self):
        """A v2 checkpoint chains to v1's newest row through previous_digest.

        This is the v2-must-cross-sign-v1 property: the first v2 signature
        commits to the last v1 signature, so a reader cannot be shown a v1
        history and a v2 history that are each self-consistent but different.
        """
        v1_checkpoint = Checkpoint(
            tree_size=2,
            merkle_root=merkle_root([b"\x01" * 32, b"\x02" * 32]),
            chain_hash=b"\x03" * 32,
            timestamp=datetime(2026, 10, 1, tzinfo=UTC),
        )
        v1_signer = generate_ed25519_signer(seed=_seed("v1-signer"))
        v1_signed = sign_checkpoint(v1_checkpoint, v1_signer)

        v2_signer = generate_ed25519_signer(seed=_seed("v2-signer"))
        v2_checkpoint = _v2_checkpoint(tree_size=2, previous_digest=bytes.fromhex(checkpoint_digest(v1_signed)))
        v2_signed = sign_checkpoint(v2_checkpoint, v2_signer)

        assert v2_signed.checkpoint.previous_digest.hex() == checkpoint_digest(v1_signed)
        # And the v2 note text commits to it, so the link is inside the signature.
        assert checkpoint_digest(v1_signed) in v2_signed.checkpoint.note_text()
        assert verify_checkpoint(v2_signed, v2_signer)
        assert verify_checkpoint(v1_signed, v1_signer)  # the v1 row still verifies

    def test_signing_a_v2_checkpoint_without_a_public_key_is_refused(self):
        signer = _OpaqueSigner()
        with pytest.raises(ValueError, match="public_key_bytes"):
            sign_checkpoint(_v2_checkpoint(), signer)


class _OpaqueSigner:
    """A Signer with no public key, standing in for a dev/custom signer."""

    algorithm = "ed25519"
    key_id = "opaque"

    def sign(self, message: bytes) -> bytes:
        return hashlib.sha256(message).digest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        return self.sign(message) == signature


class Ed25519LikeSigner(_OpaqueSigner):
    """Wraps a real Ed25519 signer but claims another key's key_id."""

    def __init__(self, inner, key_id):
        self._inner = inner
        self.key_id = key_id
        self.key_name = inner.key_name

    def sign(self, message: bytes) -> bytes:
        return self._inner.sign(message)

    def verify(self, message: bytes, signature: bytes) -> bool:
        return self._inner.verify(message, signature)

    def public_key_bytes(self) -> bytes:
        return self._inner.public_key_bytes()


class TestKeyBounds:
    """Validity bounds are enforced, and an unbounded key is called out."""

    def _raw(self, **extra) -> str:
        spec = {"algorithm": "ed25519", "public_key": "ab" * 32}
        spec.update(extra)
        return json.dumps({"k1": spec})

    def test_bounds_are_parsed(self):
        keys = load_trusted_keys(
            self._raw(not_before="2026-01-01T00:00:00Z", not_after="2027-01-01T00:00:00Z", max_tree_size=100)
        )
        key = keys["k1"]
        assert key.not_before == datetime(2026, 1, 1, tzinfo=UTC)
        assert key.not_after == datetime(2027, 1, 1, tzinfo=UTC)
        assert key.max_tree_size == 100
        assert key.has_bounds()

    def test_a_key_with_no_bounds_is_unbounded_and_says_so(self, caplog):
        with caplog.at_level("WARNING"):
            keys = load_trusted_keys(self._raw())
        assert not keys["k1"].has_bounds()
        assert any("no validity bounds" in record.getMessage() for record in caplog.records)

    def test_an_unparseable_bound_refuses_the_whole_store(self):
        """F6 INVERTED THIS TEST. It used to be
        `test_an_unparseable_bound_is_ignored_not_fatal` and asserted that
        not_before=None and max_tree_size=None -- i.e. that a bound the operator
        wrote and got wrong was silently dropped.

        Dropping it is not "no bound", it is "no bound, because the bound you
        wrote could not be read". The two look identical to every downstream
        check, and the consequence is that a key the operator intended to expire
        verifies forever. F6 fails the load closed instead: no trust store means
        nothing verifies, and the signer refuses loudly rather than publishing to
        a key that was never actually bounded.
        """
        with pytest.raises(TrustedKeyError, match="not_before"):
            load_trusted_keys(self._raw(not_before="yesterday", max_tree_size="many"))
        with pytest.raises(TrustedKeyError, match="max_tree_size"):
            load_trusted_keys(self._raw(max_tree_size="many"))

    def test_an_absent_bound_is_still_absent_not_fatal(self):
        """The other half of F6, and the reason it is not just "raise on
        everything": a key with no bound written is unbounded on purpose. That is
        a first key's normal state and must keep loading.
        """
        keys = load_trusted_keys(self._raw())
        assert keys["k1"].not_before is None
        assert keys["k1"].not_after is None
        assert keys["k1"].max_tree_size is None

    def test_an_inverted_window_refuses_the_whole_store(self):
        """F6 CHANGED THIS from "refused entirely" returning {} with a warning.

        It already refused that key; F6's point is that refusing SILENTLY is the
        same failure as not refusing. An empty store and a store that failed to
        load look identical to the signer, so the operator never learns their
        published key document is broken. It raises now.
        """
        with pytest.raises(TrustedKeyError, match="not_before is later"):
            load_trusted_keys(
                self._raw(
                    not_before="2027-01-01T00:00:00Z",
                    not_after="2026-01-01T00:00:00Z",
                )
            )

    def test_bounds_are_checked_against_the_checkpoints_own_fields(self):
        key = TrustedKey(
            "k1",
            "ed25519",
            b"\x01" * 32,
            not_before=datetime(2026, 1, 1, tzinfo=UTC),
            not_after=datetime(2026, 2, 1, tzinfo=UTC),
            max_tree_size=10,
        )
        inside = datetime(2026, 1, 15, tzinfo=UTC)
        assert key.is_within_bounds(10, inside)[0] is True
        assert key.is_within_bounds(11, inside)[0] is False
        assert key.is_within_bounds(1, datetime(2025, 12, 31, tzinfo=UTC))[0] is False
        assert key.is_within_bounds(1, datetime(2026, 3, 1, tzinfo=UTC))[0] is False

    @needs_cryptography
    def test_verifier_for_returns_none_outside_the_validity_window(self):
        signer = generate_ed25519_signer(seed=_seed("bounded"))
        checkpoint = _v2_checkpoint()
        signed = sign_checkpoint(checkpoint, signer)
        inside = TrustedKey(
            signer.key_id, "ed25519", signer.public_key_bytes(),
            not_before=datetime(2026, 1, 1, tzinfo=UTC),
            not_after=datetime(2027, 1, 1, tzinfo=UTC),
        )
        assert verifier_for(signed, {signer.key_id: inside}) is not None

        expired = TrustedKey(
            signer.key_id, "ed25519", signer.public_key_bytes(),
            not_after=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert verifier_for(signed, {signer.key_id: expired}) is None

    @needs_cryptography
    def test_a_key_name_mismatch_defeats_verification(self):
        signer = generate_ed25519_signer(seed=_seed("named"))
        signed = sign_checkpoint(_v2_checkpoint(), signer)
        wrong_name = TrustedKey(signer.key_id, "ed25519", signer.public_key_bytes(), key_name="other.test/log")
        assert verifier_for(signed, {signer.key_id: wrong_name}) is None
        right_name = TrustedKey(signer.key_id, "ed25519", signer.public_key_bytes(), key_name=ORIGIN)
        assert verifier_for(signed, {signer.key_id: right_name}) is not None


class TestSignerRefusals:
    """Append-only enforcement, idempotency, and no tree-size regression."""

    @needs_cryptography
    async def test_signs_a_v2_checkpoint_and_chains_it(self, db_session):
        log = await _log_with(4)
        signer = generate_ed25519_signer(seed=_seed("signer-1"))
        result = await signing.sign_next_checkpoint(
            db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert result.status == "signed"
        assert result.tree_size == 4
        assert result.previous_digest is None
        assert result.signed is not None
        # F9 moved the signer's published format to v3 (the RFC 6962 note). This
        # test's contract is that the run signs and chains, not which label it
        # carries, so it asserts the signer's own format constant.
        assert result.signed.checkpoint.format == signing.SIGNER_FORMAT
        assert verify_checkpoint(result.signed, signer)
        # The note a third party would check.
        document = result.signed.signed_note_document()
        assert document is not None and document.startswith(ORIGIN + "\n4\n")

        # Second run over new entries chains to the first.
        await log.append({"type": "test", "n": "4"})
        second = await signing.sign_next_checkpoint(
            db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert second.status == "signed"
        assert second.previous_digest == checkpoint_digest(result.signed)
        assert second.signed.checkpoint.previous_digest.hex() == checkpoint_digest(result.signed)

    @needs_cryptography
    async def test_rerunning_with_no_new_entries_is_idempotent(self, db_session):
        log = await _log_with(3)
        signer = generate_ed25519_signer(seed=_seed("signer-2"))
        first = await signing.sign_next_checkpoint(db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True)
        again = await signing.sign_next_checkpoint(db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True)
        assert again.status == "already_signed"
        assert again.merkle_root == first.merkle_root
        # Exactly one row exists: idempotent means no second insert.
        rows = await db_session.execute(
            __import__("sqlalchemy").select(TransparencyCheckpoint)
        )
        assert len(rows.scalars().all()) == 1

    @needs_cryptography
    async def test_a_v1_row_does_not_block_the_first_v2_checkpoint(self, db_session):
        """The migration case, and the bug a live dev run found.

        v1 rows are append-only, so the v2 signer cannot rewrite the one
        checkpoint signed under HMAC -- it has to publish a v2 checkpoint at the
        same tree size and chain to the v1 row through previous_digest. If a v1
        row counted as "already signed", the log would stay on HMAC signatures
        for good while the cron reported success on every single run.
        """
        log = await _log_with(3)
        signer = generate_ed25519_signer(seed=_seed("signer-v1-then-v2"))

        # The historical v1 row, signed the way the pre-v2 code signed.
        v1 = Checkpoint(
            tree_size=3,
            merkle_root=merkle_root([e.leaf_hash for e in await log.entries()]),
            chain_hash=(await log.entries())[-1].chain_hash,
            timestamp=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        )
        v1_signed = sign_checkpoint(v1, HmacDevSigner(b"legacy-dev-secret", key_id="dev-hmac-legacy"))
        v1_row = await save_checkpoint(db_session, v1_signed)
        v1_digest = checkpoint_digest(v1_signed)

        # F1 CHANGED THIS TEST. An HMAC-signed v1 row cannot be authenticated
        # by any public key -- keys.py refuses to admit an HMAC into the trusted
        # set at all, because it would be verification theater on a proof page --
        # so the signer now refuses to build on it, permanently, and the row is
        # append-only so it cannot be removed. Quarantining it is the supported
        # way forward: the row stays, the signer stops trusting it, and no weak
        # verification path is kept alive for it.
        unquarantined = await signing.sign_next_checkpoint(
            db_session, log, signer, origin=ORIGIN, lock=False,
            trusted_keys=_trusted(signer), genesis_confirmed=True,
        )
        assert unquarantined.status == "refused"
        assert unquarantined.reason == signing.REFUSAL_PREVIOUS_KEY_UNKNOWN

        db_session.add(
            TransparencyQuarantine(checkpoint_id=str(v1_row.id), reason="pre-v2 HMAC row")
        )
        await db_session.flush()

        result = await signing.sign_next_checkpoint(
            db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert result.status == "signed", result.reason
        assert result.tree_size == 3
        assert result.signed is not None
        assert result.signed.checkpoint.format == signing.SIGNER_FORMAT
        # F9 CHANGED THIS ASSERTION, and the change is the finding. The v1 row
        # above carries the legacy CT root over these same leaves; the new
        # checkpoint carries the RFC 6962 root. Those are different numbers BY
        # DESIGN -- the legacy construction is the one where [a,b,c] and
        # [a,b,c,c] collide -- which is exactly why the scheme has to travel
        # with the root and why tree_scheme is NOT NULL in the v3 migration.
        assert result.signed.checkpoint.merkle_root.hex() == rfc6962_root(
            [e.leaf_hash for e in await log.entries()]
        ).hex()
        assert result.signed.checkpoint.merkle_root.hex() != v1.merkle_root.hex()
        # previous_digest is None, NOT the v1 digest, and that is the honest
        # consequence of quarantining: a quarantined row is excluded from the
        # chain entirely, so the first v2 checkpoint after it is the first
        # checkpoint of the trusted chain. The operator then records THIS one as
        # the external head. Chaining to a row we just declared untrustworthy
        # would launder that declaration.
        assert result.signed.checkpoint.previous_digest is None
        assert v1_digest is not None  # the v1 digest was still computed above
        # Two rows at the same size, one per format: that is the design, not a bug.
        rows = await db_session.execute(__import__("sqlalchemy").select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 2

        # And the second v2 run is a genuine no-op, not another row.
        again = await signing.sign_next_checkpoint(
            db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert again.status == signing.REFUSAL_ALREADY_SIGNED
        rows = await db_session.execute(__import__("sqlalchemy").select(TransparencyCheckpoint))
        assert len(rows.scalars().all()) == 2
        # already_signed hands back the row it found, so the caller can publish
        # a note.
        assert again.signed is not None
        assert again.signed.checkpoint.format == signing.SIGNER_FORMAT
        assert again.signed.signed_note_document() is not None

    @needs_cryptography
    async def test_refuses_to_sign_a_shorter_history(self, db_session):
        """A log that shrank must not produce a valid-looking new checkpoint."""
        log = await _log_with(5)
        signer = generate_ed25519_signer(seed=_seed("signer-3"))
        await signing.sign_next_checkpoint(db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True)

        # Rebuild the log with fewer entries, as a truncation would.
        shorter = await _log_with(3)
        result = await signing.sign_next_checkpoint(
            db_session, shorter, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_LOG_SMALLER
        assert result.signed is None

    @needs_cryptography
    async def test_refuses_when_the_published_prefix_no_longer_derives(self, db_session):
        """The append-only property, enforced by re-derivation.

        A log covering the same size but hashing differently is exactly the
        split-brain case an append-only trigger alone does not catch: no row was
        mutated, a second history was simply built. The signer re-derives the
        root over the first N leaves and refuses when it disagrees with the
        root already published at that size.
        """
        signer = generate_ed25519_signer(seed=_seed("signer-4"))
        first = await _log_with(3)
        await signing.sign_next_checkpoint(db_session, first, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True)

        # A different log of the same length: entry 1's payload differs, so the
        # root over the first 3 entries no longer matches what was published.
        forged = InMemoryMerkleLog()
        for i in range(3):
            await forged.append({"type": "test", "n": "forged" if i == 1 else str(i)})

        result = await signing.sign_next_checkpoint(
            db_session, forged, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert result.status == "refused"
        assert result.reason in (signing.REFUSAL_EQUIVOCATION, signing.REFUSAL_INCONSISTENT_HISTORY)
        assert result.signed is None

    @needs_cryptography
    async def test_refuses_equivocation_at_a_published_tree_size(self, db_session):
        """A second row at the same size with a different root is refused.

        The rogue row is signed by a SECOND PUBLISHED key, on purpose. An
        unauthenticated row is caught earlier, by F1, with a different code --
        which is correct, but it would mean this test no longer measured
        equivocation at all. Signing it with a key the operator really did
        publish isolates the check under test: two validly-signed checkpoints
        claiming the same tree_size with different roots.
        """
        signer = generate_ed25519_signer(seed=_seed("signer-5"))
        rogue = generate_ed25519_signer(seed=_seed("signer-5-rogue"))
        log = await _log_with(2)
        await signing.sign_next_checkpoint(db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True)

        # Same tree_size, different root, validly signed by a published key: a
        # compromised second key or a rogue rotation, not a forger.
        entries = await log.entries()
        bogus = Checkpoint(
            tree_size=2,
            merkle_root=b"\xaa" * 32,
            chain_hash=entries[-1].chain_hash,
            timestamp=datetime.now(UTC),
            origin=ORIGIN,
            format=FORMAT_C2SP_V2,
        )
        await save_checkpoint(db_session, sign_checkpoint(bogus, rogue))
        await db_session.flush()

        result = await signing.sign_next_checkpoint(
            db_session, log, rogue, origin=ORIGIN, lock=False,
            trusted_keys={**_trusted(signer), **_trusted(rogue)}, genesis_confirmed=True,
        )
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_EQUIVOCATION
        assert result.detail["published_root"] != result.detail["derived_root"]

    async def test_an_empty_log_is_refused(self, db_session):
        signer = generate_ed25519_signer(seed=_seed("signer-6"))
        result = await signing.sign_next_checkpoint(
            db_session, InMemoryMerkleLog(), signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True
        )
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_EMPTY_LOG

    @needs_cryptography
    async def test_the_signer_refuses_a_log_whose_chain_is_broken(self, db_session):
        """build_checkpoint's chain check still runs inside the signer."""
        import dataclasses

        log = await _log_with(3)
        signer = generate_ed25519_signer(seed=_seed("signer-7"))
        # Corrupt the second entry's chain hash: the chain no longer verifies.
        # LogEntry is frozen, so the entry is replaced rather than mutated --
        # which is itself the point: the log gives no way to edit one in place.
        log._entries[1] = dataclasses.replace(log._entries[1], chain_hash=b"\xff" * 32)
        result = await signing.sign_next_checkpoint(db_session, log, signer, origin=ORIGIN, lock=False, trusted_keys=_trusted(signer), genesis_confirmed=True)
        # F10 CHANGED THIS TEST. This used to assert that build_checkpoint's
        # ValueError escaped sign_next_checkpoint, which was the bug: a tampered
        # log became an unhandled 500 with nothing on the alert table, so the
        # operator saw "server error" and every later run failed identically with
        # no record of why. The chain check still runs -- that is what this test
        # is for -- but its ValueError is now caught and reported as a refusal,
        # which is the observable difference.
        assert result.status == "refused"
        assert result.reason == signing.REFUSAL_CHAIN_INVALID
        assert "intact chain" in result.detail["detail_problem"]


class TestWatchdog:
    """The dead man's switch measures the artifact, not the schedule."""

    async def test_no_checkpoint_is_unhealthy(self):
        healthy, verdict = signing.watchdog_verdict(None, max_interval_hours=26)
        assert healthy is False
        assert "no checkpoint" in verdict

    async def test_a_fresh_checkpoint_is_healthy(self):
        healthy, verdict = signing.watchdog_verdict(1.5, max_interval_hours=26)
        assert healthy is True
        assert "1.5h" in verdict

    async def test_a_stale_checkpoint_is_unhealthy(self):
        healthy, verdict = signing.watchdog_verdict(48.0, max_interval_hours=26)
        assert healthy is False
        assert "past the 26.0h interval" in verdict

    async def test_the_interval_boundary_is_inclusive(self):
        assert signing.watchdog_verdict(26.0, max_interval_hours=26)[0] is True

    async def test_age_is_measured_from_the_published_timestamp(self, db_session):
        old = datetime.now(UTC) - timedelta(hours=30)
        db_session.add(
            TransparencyCheckpoint(
                tree_size=4,
                merkle_root="ab" * 32,
                chain_hash="cd" * 32,
                timestamp=old,
                signature="00" * 32,
                algorithm="ed25519",
                key_id="k",
            )
        )
        await db_session.flush()
        age = await signing.checkpoint_age_hours(db_session)
        assert 29.5 < age < 30.5
        assert signing.watchdog_verdict(age, max_interval_hours=26)[0] is False
