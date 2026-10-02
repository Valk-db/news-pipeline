"""C2SP signed-note primitives: https://c2sp.org/signed-note.

A signed note is the smallest thing both halves of the ecosystem already agree
on: text, a blank line, then one line per signature. Nothing else is required,
so an auditor can check a checkpoint with a hundred lines of Python and no
library, which is the entire point of preferring it over a bespoke envelope.

The wire format, as specified:

    <note text>          ends in a newline; the note text INCLUDES that newline
    <blank line>
    -- <key name> <base64 signature>\\n      (one line per signature)

The signature is base64 of 4+n bytes: a uint32 big-endian key id followed by
the raw signature over the note text. The recommended key id is the first four
bytes of

    SHA-256(UTF8(key name) || 0x0A || signature-type-byte || public key)

and signature type 0x01 is Ed25519 over the note text with a 32-byte public
key. Verifiers MUST ignore signatures from keys they do not know -- that is what
lets a witness cosign later -- and MUST reject the note when no known key's
signature verifies. Both rules are in verify_note_signatures().

A verifier key is encoded "<key name>+<hex key id>+<base64(type || public key)>".
The '+' separator is why a key name may not contain '+', and a key name may not
contain whitespace either; both are enforced rather than assumed.

Deliberately dependency-free and I/O-free: this module hashes and formats, and
nothing else. It does not know what a checkpoint is.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import struct
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

# Signature type byte for "Ed25519 over the note text, 32-byte public key".
ED25519_NOTE_SIG_TYPE = 0x01

# The signature line is EM DASH, SPACE, key name, SPACE, base64. U+2014, not a
# hyphen: an ASCII "-" would be ambiguous with the "- " prefix conventions.
SIGNATURE_PREFIX = "\u2014 "

KEY_ID_BYTES = 4
SIGNATURE_LENGTH_BYTES = 4 + 64  # key id + a raw Ed25519 signature
ED25519_PUBLIC_KEY_BYTES = 32

# "Verifiers MUST accept at least 16 signatures" -- parse everything up to this
# many, then stop, so a hostile note cannot make a verifier allocate per line.
MAX_SIGNATURES = 64

# Signed-note notes are meant to be small; anything past this is either a bug or
# an attempt to make a verifier do unbounded work.
MAX_NOTE_BYTES = 64 * 1024


class SignedNoteError(ValueError):
    """A note is malformed. Distinct from a note that is merely unverified."""


def validate_key_name(key_name: str) -> str:
    """Check the spec's key-name rules: non-empty, no whitespace, no '+'.

    '+' is the field separator in the verifier-key encoding, and whitespace
    would make the signature line ambiguous to a line-oriented parser. Both
    would be a self-inflicted interoperability bug, so they are refused at the
    point of construction rather than discovered by a third party.
    """
    if not isinstance(key_name, str) or not key_name:
        raise SignedNoteError("key name must be a non-empty string")
    if "+" in key_name:
        raise SignedNoteError(f"key name {key_name!r} may not contain '+'")
    if any(ch.isspace() for ch in key_name):
        raise SignedNoteError(f"key name {key_name!r} may not contain whitespace")
    return key_name


def note_key_id(
    key_name: str,
    public_key: bytes,
    *,
    signature_type: int = ED25519_NOTE_SIG_TYPE,
) -> bytes:
    """The RECOMMENDED 4-byte key id: SHA-256(name || 0x0A || type || key)[:4].

    Both the key name and the public key go in, so the same key used under two
    names yields two ids: rotating a key's name is how a verifier can tell two
    epochs apart without the operator reissuing the key.
    """
    validate_key_name(key_name)
    digest = hashlib.sha256(
        key_name.encode("utf-8")
        + b"\x0a"
        + bytes([signature_type])
        + bytes(public_key)
    ).digest()
    return digest[:KEY_ID_BYTES]


@dataclass(frozen=True)
class NoteSignature:
    """One signature line: who signed (key name), which key id, and the bytes."""

    key_name: str
    key_id: bytes
    signature: bytes  # raw signature bytes, no key id prefix

    def encoded(self) -> bytes:
        """Key id (uint32 BE) || signature -- the bytes the line carries."""
        return struct.pack(">I", int.from_bytes(self.key_id, "big")) + self.signature

    @classmethod
    def decode(cls, key_name: str, blob: bytes) -> NoteSignature:
        if len(blob) < KEY_ID_BYTES + 1:
            raise SignedNoteError("signature line is too short to hold a key id and a signature")
        return cls(
            key_name=validate_key_name(key_name),
            key_id=blob[:KEY_ID_BYTES],
            signature=blob[KEY_ID_BYTES:],
        )


def sign_note_text(
    note_text: str,
    private_key: object,
    *,
    key_name: str,
    public_key: bytes,
) -> NoteSignature:
    """Sign note text with an Ed25519 private key object.

    `private_key` is a cryptography Ed25519PrivateKey. It is typed as object so
    this module stays importable (and testable) with no `cryptography`
    installed; the attribute error it raises without one is honest.
    """
    validate_key_name(key_name)
    signature = bytes(private_key.sign(note_text.encode("utf-8")))  # type: ignore[attr-defined]
    return NoteSignature(
        key_name=key_name,
        key_id=note_key_id(key_name, public_key),
        signature=signature,
    )


def verify_note_signature(public_key: bytes, note_text: str, signature: bytes) -> bool:
    """True when `signature` is a valid Ed25519 signature over `note_text`.

    False for every failure mode, including a missing `cryptography`: a caller
    that gets False must render "unverified", never an exception.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - depends on the installed environment
        logger.warning("Cannot verify a signed note: the 'cryptography' package is not installed")
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(
            bytes(signature), note_text.encode("utf-8")
        )
    except Exception:
        return False
    return True


def encode_signed_note(note_text: str, signatures: Sequence[NoteSignature]) -> str:
    """Render the document: note text, blank line, one line per signature.

    The note text must already end in a newline (that is part of what gets
    signed). A document with no signatures is refused: publishing an unsigned
    note would look like a signed one to a careless reader.
    """
    if not signatures:
        raise SignedNoteError("a signed note needs at least one signature")
    body = note_text if note_text.endswith("\n") else note_text + "\n"
    # body already ends in a newline, so the blank separator is one more: the
    # document is "text\n" + "\n" + signature lines. Dropping that second
    # newline is not a cosmetic slip, it produces a document whose note text
    # swallows the first signature line and which no conforming parser splits.
    lines = [body, "\n"]
    for sig in signatures:
        validate_key_name(sig.key_name)
        lines.append(f"{SIGNATURE_PREFIX}{sig.key_name} {base64.b64encode(sig.encoded()).decode('ascii')}\n")
    return "".join(lines)


@dataclass(frozen=True)
class ParsedNote:
    """A parsed document: the signed text, plus every signature line found."""

    note_text: str
    signatures: tuple[NoteSignature, ...]

    def signature_for(self, key_name: str) -> NoteSignature | None:
        """The first signature line from `key_name`, if the note carries one."""
        for sig in self.signatures:
            if sig.key_name == key_name:
                return sig
        return None

    def key_names(self) -> tuple[str, ...]:
        return tuple(sig.key_name for sig in self.signatures)


def parse_signed_note(document: str) -> ParsedNote:
    """Split a signed-note document into its note text and its signatures.

    Per the spec, the note text is everything before the blank line and
    includes its own trailing newline; the signatures are the lines after it.
    A line that is not a well-formed signature line is dropped with a warning
    rather than failing the parse, because the same rules require a verifier to
    ignore what it does not understand -- and a document with zero usable
    signature lines is rejected outright, since that is exactly the case the
    spec says must be refused.
    """
    if len(document.encode("utf-8")) > MAX_NOTE_BYTES:
        raise SignedNoteError(f"note document exceeds {MAX_NOTE_BYTES} bytes")
    if "\n\n" not in document:
        raise SignedNoteError("not a signed note: no blank line separates the text from the signatures")
    note_text, _, tail = document.partition("\n\n")
    # The note text includes its own trailing newline: that newline is part of
    # the bytes that were signed, so it must survive the split. (A text that
    # lost it -- a hand-built document -- gets it back before anything verifies.)
    if not note_text.endswith("\n"):
        note_text += "\n"

    signatures: list[NoteSignature] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(signatures) >= MAX_SIGNATURES:
            logger.warning("Ignoring signatures past the %d-signature cap", MAX_SIGNATURES)
            break
        if not line.startswith(SIGNATURE_PREFIX):
            logger.warning("Ignoring note line that is not a signature line")
            continue
        remainder = line[len(SIGNATURE_PREFIX):]
        key_name, _, encoded = remainder.partition(" ")
        try:
            validate_key_name(key_name)
            blob = base64.b64decode(encoded, validate=True)
            signatures.append(NoteSignature.decode(key_name, blob))
        except (SignedNoteError, binascii.Error) as exc:
            logger.warning("Ignoring malformed signature line from %r: %s", key_name, exc)
    if not signatures:
        raise SignedNoteError("not a signed note: no signature line could be read")
    return ParsedNote(note_text=note_text, signatures=tuple(signatures))


def verify_note_signatures(
    parsed: ParsedNote,
    known_keys: dict[str, bytes],
    *,
    require_key_ids: bool = True,
) -> list[str]:
    """Return the names of known keys whose signature verifies. [] means reject.

    `known_keys` maps key name to raw public key bytes. Signatures from unknown
    names are ignored, as the spec requires, and at least one known key must
    verify or the caller must refuse the note.
    """
    verified: list[str] = []
    for sig in parsed.signatures:
        public_key = known_keys.get(sig.key_name)
        if public_key is None:
            logger.info("Ignoring note signature from unknown key %r", sig.key_name)
            continue
        if require_key_ids and note_key_id(sig.key_name, public_key) != sig.key_id:
            logger.warning(
                "Note signature from %r carries key id %s, but the public key derives %s",
                sig.key_name, sig.key_id.hex(), note_key_id(sig.key_name, public_key).hex(),
            )
            continue
        if verify_note_signature(public_key, parsed.note_text, sig.signature):
            verified.append(sig.key_name)
    return verified


def encode_verifier_key(
    key_name: str,
    public_key: bytes,
    *,
    signature_type: int = ED25519_NOTE_SIG_TYPE,
) -> str:
    """The spec's verifier-key form: "<name>+<hex key id>+<base64(type || key)>"."""
    validate_key_name(key_name)
    key_id = note_key_id(key_name, public_key, signature_type=signature_type)
    blob = bytes([signature_type]) + bytes(public_key)
    return f"{key_name}+{key_id.hex()}+{base64.b64encode(blob).decode('ascii')}"


def decode_verifier_key(text: str) -> tuple[str, bytes, bytes]:
    """Inverse of encode_verifier_key -> (key name, key id bytes, public key)."""
    parts = text.split("+")
    if len(parts) != 3:
        raise SignedNoteError("verifier key must be '<name>+<hex key id>+<base64>'")
    name, key_id_hex, blob_b64 = parts
    validate_key_name(name)
    try:
        key_id = bytes.fromhex(key_id_hex)
        blob = base64.b64decode(blob_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SignedNoteError(f"verifier key is not decodable: {exc}") from exc
    if len(key_id) != KEY_ID_BYTES:
        raise SignedNoteError(f"verifier key id must be {KEY_ID_BYTES} bytes, got {len(key_id)}")
    if len(blob) != 1 + ED25519_PUBLIC_KEY_BYTES:
        raise SignedNoteError("verifier key blob must be one type byte plus a 32-byte public key")
    return name, key_id, blob[1:]
