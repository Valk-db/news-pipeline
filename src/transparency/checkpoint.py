"""Signed checkpoints over the first N entries of the log.

A checkpoint is the artifact a third party can hold:

    tree_size    how many entries the root covers
    merkle_root  root over those entries' leaf hashes
    chain_hash   the chain hash of the last covered entry
    timestamp    when the operator signed

Signing is the point of this module, and so is what signing does *not* prove. A
signature shows the checkpoint came from the key holder and has not been edited
since. It does not stop the key holder from signing a different history
tomorrow. That second gap is closed by anchoring.py handing the root digest to
an outside party with its own clock.

Key handling: `cryptography` is not a declared dependency of this repo, so the
Ed25519 path imports lazily and reports ed25519_available() == False when the
package is absent. HmacDevSigner exists so the pipeline stays runnable and
testable without it, and it is NOT a substitute for a signature: the operator
holds the key, so it proves nothing to anybody else. It is marked
NOT FOR PRODUCTION in the class docstring and in scripts/run_transparency_demo.py.

Three signed formats live here, and a checkpoint carries the name of the one it
was signed with:

    FORMAT_JSON_V1 = "n1-json-v1"          this project's original envelope
    FORMAT_C2SP_V2 = "c2sp-tlog-checkpoint-v2"   a C2SP signed note, CT tree
    FORMAT_C2SP_V3 = "c2sp-tlog-checkpoint-v3"   a C2SP signed note, RFC 6962 tree

v1 is a JSON object behind the domain separator CHECKPOINT_DOMAIN. It is
project-specific: nothing outside this repo knows how to read it, so a
third-party verifier has to take our word for the parser. v2 is
https://c2sp.org/tlog-checkpoint: three mandatory lines (origin, tree size,
root hash) plus signature lines in the signed-note format, which any auditor
can check without a library. New checkpoints are v3; v1 and v2 stay
byte-for-byte readable because rows signed under them are already published and
re-verifying them must keep working.

The one deviation from tlog-checkpoint v1.0.0: that format has no timestamp and
no link to the previous checkpoint, and both matter here (a checkpoint without
a time cannot be aged by a watchdog, and one without a previous-digest link
cannot be chained by a reader). So v2 appends exactly ONE extension line
carrying them. The spec permits extension lines but says they are NOT
RECOMMENDED because log monitors cannot audit them, so the three mandatory
lines carry the whole verifiable claim and the extension is additive: a monitor
that ignores it still gets origin, size and root.

Why v3 exists (F9, F8, F15 -- all three change the signed bytes, so they land
as one format rather than three):

- v2 hashed the tree the Certificate Transparency way: an odd level DUPLICATED
  its last node, and leaves carried no prefix at all, so an interior node and a
  leaf shared a hash domain. That is CVE-2012-2459: two different histories
  ([a,b,c] and [a,b,c,c]) produce the same root, so append-only is not actually
  enforced. v3 uses RFC 6962 leaf/internal hashes (0x00 / 0x01 prefixes) and
  promotes the odd node instead of duplicating it.
- DECISIONS.md requires the signed payload to carry log identifier, key_id,
  tree size, root and timestamp. v2's payload carried no key id at all, and
  nothing bound the log's origin to the key name at signing time. v3's extension
  line carries key_id, and sign/verify both refuse when origin != key_name.
- v2's payload committed to nothing about the per-entry timestamps, so editing
  an entry's timestamp column left every published signature valid. v3 carries
  a commitment to the covered entries' (index, timestamp) pairs.

A v2 root and a v3 root are different numbers over the same leaves, so the
format travels with the root: transparency_checkpoints.tree_scheme records which
tree produced a given row, and a reader that finds v3 bytes with a v2 scheme
refuses rather than comparing incomparable hashes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from src.transparency.log import GENESIS_CHAIN_HASH, MerkleLog, canonical_json, verify_chain
from src.transparency.signed_note import (
    ED25519_NOTE_SIG_TYPE,
    KEY_ID_BYTES,
    NoteSignature,
    SignedNoteError,
    encode_signed_note,
    note_key_id,
)

logger = logging.getLogger(__name__)

# Domain separator so a checkpoint signature can never be replayed as a
# signature over some other document this project might start signing.
CHECKPOINT_DOMAIN = b"n1:merkle-checkpoint:v1"

# The signed formats a Checkpoint can carry. See the module docstring.
FORMAT_JSON_V1 = "n1-json-v1"
FORMAT_C2SP_V2 = "c2sp-tlog-checkpoint-v2"
FORMAT_C2SP_V3 = "c2sp-tlog-checkpoint-v3"
CHECKPOINT_FORMATS = (FORMAT_JSON_V1, FORMAT_C2SP_V2, FORMAT_C2SP_V3)
# Formats whose signed form is a C2SP signed note (i.e. carry a note key id).
NOTE_FORMATS = (FORMAT_C2SP_V2, FORMAT_C2SP_V3)

# Which tree hashing a format's root is computed with. The value is stored in
# transparency_checkpoints.tree_scheme so an old-scheme root can never be
# compared against, or mistaken for, a new-scheme one.
TREE_SCHEME_CT_V1 = "n1-ct-dup-v1"
TREE_SCHEME_RFC6962 = "rfc6962-sha256"
TREE_SCHEME_FOR_FORMAT = {
    FORMAT_JSON_V1: TREE_SCHEME_CT_V1,
    FORMAT_C2SP_V2: TREE_SCHEME_CT_V1,
    FORMAT_C2SP_V3: TREE_SCHEME_RFC6962,
}

# Origin written into a v2 checkpoint when the caller does not name one. The
# spec wants a unique log identity, schema-less, no spaces and no '+'; this is
# that for this log. It is also the default signer key name, since the spec
# says a key name SHOULD match the origin.
DEFAULT_ORIGIN = "procmon.dev/transparency"

# Marks the extension line inside a v2 note text, so a reader can find it
# without guessing at the line's shape.
EXTENSION_PREFIX = "x-news-pipeline "

# RFC 6962 domain separation. Leaves hash as 0x00 || leaf and interior nodes as
# 0x01 || left || right, so no leaf can ever be reinterpreted as an interior
# node (second preimage across the two levels). The old scheme prefixed only
# interior nodes, which is the v2 defect this replaces.
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

# Root of a zero-leaf tree. Only reachable for an empty log, which should never
# be signed, but the function must be total. RFC 6962 §2.1 defines it as the
# hash of the empty string, which is also what the CT tree used, so the two
# schemes agree on the empty case and differ everywhere else.
EMPTY_TREE_ROOT: bytes = hashlib.sha256(b"").digest()

ED25519_ALGORITHM = "ed25519"
HMAC_DEV_ALGORITHM = "hmac-sha256-dev"

# `cryptography` is deliberately not a declared dependency: the Vercel bundle is
# size-capped and this package is not on the request path. The import failure is
# the switch, not an error -- ed25519_available() reports which path is live.
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore[import-not-found]
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _ED25519_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the installed environment
    _ED25519_AVAILABLE = False


def ed25519_available() -> bool:
    """True when the optional `cryptography` package can be used."""
    return _ED25519_AVAILABLE


def rfc6962_leaf_hash(leaf: bytes) -> bytes:
    """RFC 6962 leaf hash: sha256(0x00 || leaf)."""
    return hashlib.sha256(LEAF_PREFIX + leaf).digest()


def rfc6962_node_hash(left: bytes, right: bytes) -> bytes:
    """RFC 6962 interior hash: sha256(0x01 || left || right)."""
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def merkle_levels(leaf_hashes: Sequence[bytes]) -> list[list[bytes]]:
    """LEGACY (v1/v2, TREE_SCHEME_CT_V1): the CT tree, duplicating the last node.

    Kept only so an already-published v1/v2 root can be re-derived and
    re-verified byte for byte. DO NOT use it for anything new: an odd level
    repeats its final node, so [a,b,c] and [a,b,c,c] share a root (the
    CVE-2012-2459 ambiguity), and leaves carry no prefix so a leaf and an
    interior node share a hash domain. New code uses rfc6962_levels.
    """
    if not leaf_hashes:
        return []
    level = list(leaf_hashes)
    levels = [level]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(NODE_PREFIX + left + right).digest() for left, right in zip(level[::2], level[1::2])]
        levels.append(level)
    return levels


def merkle_root(leaf_hashes: Sequence[bytes]) -> bytes:
    """LEGACY root (TREE_SCHEME_CT_V1). See merkle_levels; new code uses rfc6962_root."""
    levels = merkle_levels(leaf_hashes)
    return levels[-1][0] if levels else EMPTY_TREE_ROOT


def root_for_scheme(leaf_hashes: Sequence[bytes], scheme: str) -> bytes:
    """Root over the given leaf hashes under the named scheme."""
    if scheme == TREE_SCHEME_RFC6962:
        return rfc6962_root(leaf_hashes)
    if scheme == TREE_SCHEME_CT_V1:
        return merkle_root(leaf_hashes)
    raise ValueError(f"unknown tree scheme {scheme!r}")


def rfc6962_levels(leaf_hashes: Sequence[bytes]) -> list[list[bytes]]:
    """Build the RFC 6962 tree bottom-up, promoting the odd node.

    An odd level does NOT duplicate its last node: the final node is carried up
    unchanged to be the right-hand child of the next level's last pair. That is
    what makes the tree injective in the leaf sequence -- every distinct history
    gets a distinct root -- and it is why a proof path has a variable length.
    """
    if not leaf_hashes:
        return []
    level = [rfc6962_leaf_hash(leaf) for leaf in leaf_hashes]
    levels = [level]
    while len(level) > 1:
        next_level = [
            rfc6962_node_hash(left, right) for left, right in zip(level[::2], level[1::2])
        ]
        if len(level) % 2:
            # Promote, never duplicate. Duplicating is the CT bug this replaces.
            next_level.append(level[-1])
        level = next_level
        levels.append(level)
    return levels


def rfc6962_root(leaf_hashes: Sequence[bytes]) -> bytes:
    """RFC 6962 root over the given leaf hashes, or EMPTY_TREE_ROOT for none."""
    levels = rfc6962_levels(leaf_hashes)
    return levels[-1][0] if levels else EMPTY_TREE_ROOT


def rfc6962_path(leaf_hashes: Sequence[bytes], index: int) -> list[bytes]:
    """The RFC 6962 audit path for `index`, bottom-up, over these leaf hashes.

    The path length varies with the position: a promoted odd node has no sibling
    at its own level, so a verifier must not assume ceil(log2(n)) steps. Use
    rfc6962_path_length() for the expected count.
    """
    levels = rfc6962_levels(leaf_hashes)
    if not levels:
        raise ValueError("cannot build an audit path over an empty tree")
    if not 0 <= index < len(levels[0]):
        raise ValueError(f"index {index} out of range for {len(levels[0])} leaves")
    path: list[bytes] = []
    node_index = index
    for level in levels[:-1]:
        if not (len(level) % 2 and node_index == len(level) - 1):
            # Every other node pairs with its neighbour; the promoted node has
            # no sibling and contributes nothing to the path.
            path.append(level[node_index ^ 1])
        node_index //= 2
    return path


def tree_depth(tree_size: int) -> int:
    """LEGACY (CT) proof depth: ceil(log2(size)). Kept for v1/v2 proofs."""
    depth = 0
    span = 1
    while span < tree_size:
        span *= 2
        depth += 1
    return depth


def rfc6962_path_length(index: int, tree_size: int) -> int:
    """How many digests rfc6962_path returns for this index. Variable, not ceil(log2)."""
    length = 0
    node_index = index
    span = tree_size
    while span > 1:
        if span % 2 and node_index == span - 1:
            span = (span + 1) // 2
            node_index = span - 1
            continue
        length += 1
        span = (span + 1) // 2
        node_index = node_index // 2
    return length


def proof_length_for_scheme(index: int, tree_size: int, scheme: str) -> int:
    """Expected audit-path length for a scheme."""
    if scheme == TREE_SCHEME_RFC6962:
        return rfc6962_path_length(index, tree_size)
    if scheme == TREE_SCHEME_CT_V1:
        return tree_depth(tree_size)
    raise ValueError(f"unknown tree scheme {scheme!r}")


def entry_timestamps_root(entries: Sequence[Any]) -> bytes:
    """RFC 6962 root over the (index, timestamp) pair of each covered entry.

    This is what puts entry timestamps inside the signature (F15). The per-entry
    leaf hash in log.py deliberately does NOT commit to the timestamp column --
    changing that would make every already-appended entry unverifiable, and the
    log is append-only -- so the commitment lives here instead: the signed note
    carries a root over the covered entries' (index, timestamp) pairs, and any
    edit to a timestamp after the fact changes this root and breaks the
    signature.

    Residual risk, stated plainly: an audit path proves inclusion of an entry's
    leaf hash, not of its timestamp. Verifying the timestamp claim needs the
    full covered prefix, which is what verify_entry_timestamps() is for.
    """
    if not entries:
        return EMPTY_TREE_ROOT
    digests = [
        hashlib.sha256(
            canonical_json(
                {
                    "index": entry.index,
                    "timestamp": entry.timestamp.astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
        ).digest()
        for entry in entries
    ]
    return rfc6962_root(digests)



@runtime_checkable
class Signer(Protocol):
    """Keyed signer for checkpoints. Public-key algorithms split signing from
    verification by handing out a verifier object that holds no secret."""

    algorithm: str
    key_id: str

    def sign(self, message: bytes) -> bytes:
        ...

    def verify(self, message: bytes, signature: bytes) -> bool:
        ...


class HmacDevSigner:
    """NOT FOR PRODUCTION. Shared-secret HMAC-SHA256 development fallback.

    An HMAC proves nothing beyond "someone holding the shared secret signed
    this", and the operator holds that secret, so this is a placeholder for
    local runs and tests only. A published public key is what makes a checkpoint
    checkable by somebody who does not trust us -- that is why the production
    path is Ed25519 and why nothing here should ever anchor a digest.
    """

    algorithm = HMAC_DEV_ALGORITHM

    def __init__(self, secret: bytes, *, key_id: str = "dev-hmac") -> None:
        if not secret:
            raise ValueError("HmacDevSigner requires a non-empty secret")
        self._secret = secret
        self.key_id = key_id

    def sign(self, message: bytes) -> bytes:
        return hmac.new(self._secret, message, hashlib.sha256).digest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(message), signature)


class Ed25519Signer:
    """Ed25519 signer. Requires the optional `cryptography` package."""

    algorithm = ED25519_ALGORITHM

    def __init__(
        self,
        private_key: bytes,
        *,
        key_id: str | None = None,
        key_name: str = DEFAULT_ORIGIN,
    ) -> None:
        if not _ED25519_AVAILABLE:
            raise RuntimeError(
                "Ed25519Signer needs the 'cryptography' package. "
                "Install it, or use HmacDevSigner (development only)."
            )
        self._private_key = Ed25519PrivateKey.from_private_bytes(private_key)
        self._public_key = self._private_key.public_key()
        public_bytes = bytes(self._public_key.public_bytes_raw())
        self.key_id = key_id or f"ed25519:{hashlib.sha256(public_bytes).hexdigest()[:16]}"
        self._public_key_bytes = public_bytes
        # The C2SP key name is the log's identity, so it also binds a v2
        # signature to the origin in the note (see verify_checkpoint).
        self.key_name = key_name

    def sign(self, message: bytes) -> bytes:
        return bytes(self._private_key.sign(message))

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self._public_key.verify(signature, message)
        except Exception:  # InvalidSignature and malformed input both mean "no"
            return False
        return True

    def public_key_bytes(self) -> bytes:
        """Raw public key to publish alongside checkpoints."""
        return self._public_key_bytes


class Ed25519Verifier:
    """Public-key-only half of Ed25519Signer: what a verifier actually holds."""

    algorithm = ED25519_ALGORITHM

    def __init__(self, public_key: bytes, *, key_id: str = "ed25519", key_name: str | None = None) -> None:
        if not _ED25519_AVAILABLE:
            raise RuntimeError("Ed25519Verifier needs the 'cryptography' package.")
        self._public_key = Ed25519PublicKey.from_public_bytes(public_key)
        self.key_id = key_id
        self.key_name = key_name

    def sign(self, message: bytes) -> bytes:
        raise PermissionError("Ed25519Verifier holds no private key and cannot sign.")

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self._public_key.verify(signature, message)
        except Exception:  # InvalidSignature and malformed input both mean "no"
            return False
        return True

    def public_key_bytes(self) -> bytes:
        """Raw public key, for deriving a C2SP note key id."""
        return bytes(self._public_key.public_bytes_raw())


# Domain separation for key derivation. A checkpoint signature and a signing key
# are derived in the same process from values an operator pastes; without distinct
# info strings a seed reused for one purpose could produce the other.
SIGNING_KDF_INFO = b"n1:transparency-signer:ed25519:v2"

# The minimum seed length this module accepts, in bytes.
#
# This is the load-bearing part of the fix, not the KDF. Ed25519 public keys are
# published, so anyone can take the published key and ask "which 32-byte value
# hashes to this key?". With bare SHA-256 over a short pasted string -- a
# passphrase, a key name, a word from the docs -- that search is trivial and the
# operator's signing key is recoverable from a public artifact. No KDF fixes
# that on its own either: any KDF's work factor only slows a search whose input
# space is low-entropy, and 2^n is 2^n for any n below about 128.
#
# So the requirement is explicit: the seed must carry at least 32 bytes of
# randomly generated material, which is what makes the preimage search
# intractable rather than merely slower. A shorter value is REFUSED rather than
# quietly stretched, because quietly stretching a pasted passphrase is how the
# failure shipped in the first place. The key ceremony generates 32 bytes from a
# CSPRNG and publishes the derived public key through this exact function, so
# what the operator verifies is what the signer will use.
MIN_SIGNING_SEED_BYTES = 32


def derive_ed25519_private_key(seed: bytes) -> bytes:
    """Derive the 32 private bytes Ed25519 requires from >= 32 bytes of seed.

    HKDF-SHA256 (RFC 5869), extract-then-expand, with SIGNING_KDF_INFO as the
    info string. Chosen over PBKDF2/scrypt deliberately: those exist to make a
    LOW-entropy passphrase expensive, and this path refuses low-entropy seeds
    outright (MIN_SIGNING_SEED_BYTES), so the expensive-work-factor machinery
    would buy nothing here and would put a multi-second stall in a cron route.

    A fixed salt is correct here and worth being explicit about: a per-deployment
    salt would add nothing, because the value being protected is the seed, not a
    stored hash, and the attacker gets unlimited attempts against the public key
    regardless of what salt the derivation used. The domain-separated info
    string is what prevents cross-purpose derivation reuse.
    """
    if len(seed) < MIN_SIGNING_SEED_BYTES:
        raise ValueError(
            f"signing seed must be at least {MIN_SIGNING_SEED_BYTES} bytes of random "
            f"material, got {len(seed)}. A shorter value (a passphrase, a pasted key) "
            "is brute-forceable from the published Ed25519 public key, so it is refused "
            "rather than stretched. Generate 32 bytes with a CSPRNG."
        )
    # extract(salt=<fixed>, key_material=seed) -> PRK, then expand to 32 bytes.
    prk = hmac.new(b"n1:transparency-signer:hkdf-salt", seed, hashlib.sha256).digest()
    okm = hmac.new(prk, SIGNING_KDF_INFO + b"\x01", hashlib.sha256).digest()
    return okm


def generate_ed25519_signer(seed: bytes | None = None) -> Ed25519Signer:
    """Fresh Ed25519 keypair.

    `seed` must be at least MIN_SIGNING_SEED_BYTES (32) bytes of random material;
    anything shorter raises ValueError rather than being hashed up to size. That
    is the F3 fix: the published Ed25519 public key makes the derivation a
    brute-force oracle for any low-entropy seed, so the seed must actually be
    high-entropy. See derive_ed25519_private_key for why the KDF here is HKDF and
    not a deliberately slow one.

    Omit `seed` for a random key. The production ceremony generates the seed
    outside this process, passes it here once to derive the public key it
    publishes, and never stores it next to the log.
    """
    if not _ED25519_AVAILABLE:
        raise RuntimeError("generate_ed25519_signer needs the 'cryptography' package.")
    if seed is None:
        key = Ed25519PrivateKey.generate()
    else:
        key = Ed25519PrivateKey.from_private_bytes(derive_ed25519_private_key(seed))
    return Ed25519Signer(bytes(key.private_bytes_raw()))


@dataclass(frozen=True)
class Checkpoint:
    """The signed body. Frozen: nothing may change between signing and anchoring.

    tree_size, merkle_root, chain_hash and timestamp are the v1 payload and
    keep their meaning in v2 and v3. The later fields all default to values that
    reproduce v1 exactly, so every existing caller and every published row is
    unaffected by their presence:

    origin            the log's identity, the first mandatory line of a note
    previous_digest   sha256 over the previous signed checkpoint (see
                      checkpoint_digest), hex, or None for the first one
    format            which of the three signed formats signing_bytes() produces
    key_id            the signing key's id, v3 only; it is inside the signature
    entry_timestamps  RFC 6962 root over the covered entries' (index, timestamp)
                      pairs, v3 only; this is what makes the entry timestamps
                      tamper-evident (F15)
    """
    tree_size: int
    merkle_root: bytes
    chain_hash: bytes
    timestamp: datetime
    origin: str = DEFAULT_ORIGIN
    previous_digest: bytes | None = None
    format: str = FORMAT_JSON_V1
    key_id: str | None = None
    entry_timestamps: bytes | None = None

    def signing_bytes(self) -> bytes:
        """The exact bytes that get signed, per this checkpoint's format.

        Independent of the signature itself, so any verifier can reproduce them
        from the published fields. For v3 the bytes are the note text, whose
        extension line carries key_id and the entry-timestamp commitment: that
        is the payload DECISIONS.md asks for (log identifier, key_id, tree size,
        Merkle root, timestamp) with the two additions this log needs.
        """
        if self.format in NOTE_FORMATS:
            return self.note_text().encode("utf-8")
        if self.format != FORMAT_JSON_V1:
            raise ValueError(f"unknown checkpoint format {self.format!r}")
        body = {
            "chain_hash": self.chain_hash.hex(),
            "hash_scheme": "n1:sha256",
            "merkle_root": self.merkle_root.hex(),
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tree_size": self.tree_size,
        }
        return CHECKPOINT_DOMAIN + b" " + canonical_json(body)

    def tree_scheme(self) -> str:
        """Which tree hashing produced this checkpoint's root."""
        try:
            return TREE_SCHEME_FOR_FORMAT[self.format]
        except KeyError:
            raise ValueError(f"unknown checkpoint format {self.format!r}") from None

    def extension_line(self) -> str:
        """The one opaque extension line a note carries.

        canonical_json is single-line by construction, so this cannot break the
        note's line structure. `prev` is "-" for the first checkpoint, which is
        the only value that is not hex.

        v2's extension is frozen: exactly chain_hash, prev, timestamp, in that
        shape, because a byte-different extension would invalidate signatures
        already published under it. v3 adds key_id and entry_timestamps, which is
        one of the reasons v3 is a new format rather than a patch to v2.
        """
        if self.format == FORMAT_JSON_V1:
            raise ValueError("a v1 checkpoint has no extension line")
        fields: dict[str, Any] = {
            "chain_hash": self.chain_hash.hex(),
            "prev": self.previous_digest.hex() if self.previous_digest else "-",
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if self.format == FORMAT_C2SP_V3:
            if not self.key_id:
                raise ValueError("a v3 checkpoint must carry its signing key id in the signed payload")
            if self.entry_timestamps is None:
                raise ValueError("a v3 checkpoint must carry its entry-timestamp commitment")
            fields["key_id"] = self.key_id
            fields["entry_timestamps"] = self.entry_timestamps.hex()
        return EXTENSION_PREFIX + canonical_json(fields).decode("utf-8")


    def note_text(self) -> str:
        """The C2SP signed-note text: origin, tree size, root, then the extension.

        The three mandatory lines are exactly what tlog-checkpoint v1.0.0
        specifies: a non-empty origin with no spaces or '+', the tree size as
        ASCII decimal with no leading zeroes, and the root as standard RFC 4648
        base64. The trailing newline is part of the text and part of what gets
        signed.
        """
        if not self.origin:
            raise ValueError("a v2 checkpoint needs a non-empty origin")
        if any(ch.isspace() for ch in self.origin) or "+" in self.origin:
            raise ValueError(f"origin {self.origin!r} may not contain whitespace or '+'")
        if self.tree_size < 0:
            raise ValueError(f"tree_size must be >= 0, got {self.tree_size}")
        lines = [
            self.origin,
            str(self.tree_size),
            base64.b64encode(self.merkle_root).decode("ascii"),
            self.extension_line(),
        ]
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "chain_hash": self.chain_hash.hex(),
            "merkle_root": self.merkle_root.hex(),
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tree_size": self.tree_size,
        }
        if self.format != FORMAT_JSON_V1:
            # v1's dict is left alone: it is what already-published rows carry.
            data["origin"] = self.origin
            data["previous_digest"] = self.previous_digest.hex() if self.previous_digest else None
            data["format"] = self.format
        if self.format == FORMAT_C2SP_V3:
            data["key_id"] = self.key_id
            data["entry_timestamps"] = self.entry_timestamps.hex() if self.entry_timestamps else None
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Checkpoint:
        previous = data.get("previous_digest")
        entry_timestamps = data.get("entry_timestamps")
        key_id = data.get("key_id")
        return cls(
            tree_size=int(data["tree_size"]),
            merkle_root=bytes.fromhex(str(data["merkle_root"])),
            chain_hash=bytes.fromhex(str(data["chain_hash"])),
            timestamp=datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00")),
            origin=str(data.get("origin") or DEFAULT_ORIGIN),
            previous_digest=bytes.fromhex(str(previous)) if previous else None,
            format=str(data.get("format") or FORMAT_JSON_V1),
            key_id=(str(key_id) if key_id else None),
            entry_timestamps=(bytes.fromhex(str(entry_timestamps)) if entry_timestamps else None),
        )


    def describe(self) -> str:
        return f"size={self.tree_size} root={self.merkle_root.hex()[:12]} chain={self.chain_hash.hex()[:12]}"


@dataclass(frozen=True)
class SignedCheckpoint:
    """A checkpoint plus its detached signature -- what gets anchored and published."""
    checkpoint: Checkpoint
    signature: bytes
    algorithm: str
    key_id: str
    key_name: str | None = None

    def signing_bytes(self) -> bytes:
        return self.checkpoint.signing_bytes()

    def to_dict(self) -> dict[str, Any]:
        data = {
            **self.checkpoint.to_dict(),
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": self.signature.hex(),
        }
        if self.checkpoint.format in NOTE_FORMATS:
            # The note's key name travels with the signature: it is the name a
            # verifier looks the public key up under.
            data["key_name"] = self.key_name or self.checkpoint.origin
        return data


    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SignedCheckpoint:
        checkpoint = Checkpoint.from_dict(data)
        return cls(
            checkpoint=checkpoint,
            signature=bytes.fromhex(str(data["signature"])),
            algorithm=str(data["algorithm"]),
            key_id=str(data["key_id"]),
            key_name=(str(data["key_name"]) if data.get("key_name") else None),
        )

    def note_signature(self) -> NoteSignature | None:
        """The signature as a signed-note line: 4-byte key id || raw signature.

        None for a v1 checkpoint, which has no note form.
        """
        if self.checkpoint.format not in NOTE_FORMATS:
            return None

        if len(self.signature) <= KEY_ID_BYTES:
            logger.info("v2 checkpoint signature is too short to hold a note key id")
            return None
        try:
            return NoteSignature.decode(
                self.key_name or self.checkpoint.origin, self.signature
            )
        except SignedNoteError as exc:
            logger.info("v2 checkpoint signature is not a note signature: %s", exc)
            return None

    def signed_note_document(self) -> str | None:
        """The publishable C2SP signed-note document, or None for a v1 checkpoint.

        This is the artifact a third party needs: note text, blank line, then
        one signature line they can check against a public key they were given
        out of band.
        """
        signature = self.note_signature()
        if signature is None:
            return None
        try:
            return encode_signed_note(self.checkpoint.note_text(), [signature])
        except SignedNoteError as exc:
            logger.info("Cannot render signed note: %s", exc)
            return None


async def build_checkpoint(
    log: MerkleLog,
    tree_size: int,
    *,
    timestamp: datetime | None = None,
    format: str = FORMAT_JSON_V1,
    origin: str = DEFAULT_ORIGIN,
    previous_digest: bytes | None = None,
    key_id: str | None = None,
    entries: Sequence[Any] | None = None,
) -> Checkpoint:
    """Checkpoint the first `tree_size` entries. `tree_size` may be smaller than
    the log, which is how a mid-flight checkpoint stays verifiable.

    Refuses to sign a chain that does not verify: a checkpoint is the
    operator's signed statement that this history is intact, and signing a
    broken chain would launder the break into a "signed" history.

    The format fields (format, origin, previous_digest, key_id) are metadata
    about how the result will be signed, not extra inputs to the math: two
    checkpoints over the same tree_size in the same tree scheme always agree on
    root and chain hash, which is what lets the signer re-derive and compare.

    `entries` lets a caller that already loaded the log pass the covered prefix
    in (F15: the signer used to load the whole log twice per run, once here and
    once for its own re-derivation). When omitted the log is read once here.
    """
    if tree_size < 0:
        raise ValueError(f"tree_size must be >= 0, got {tree_size}")
    available = await log.size()
    if tree_size > available:
        raise ValueError(f"tree_size {tree_size} exceeds log size {available}")
    if format not in CHECKPOINT_FORMATS:
        raise ValueError(f"unknown checkpoint format {format!r}")

    if entries is None:
        covered = list(await log.entries())[:tree_size]
    else:
        covered = list(entries)[:tree_size]
        if len(covered) < tree_size:
            raise ValueError(
                f"caller passed {len(covered)} entries for a tree_size of {tree_size}"
            )
    if not verify_chain(covered):
        raise ValueError(
            f"refusing to checkpoint: the first {tree_size} log entries do not "
            "form an intact chain (rewritten payload, gap, or reordering)"
        )

    leaves = [entry.leaf_hash for entry in covered]
    scheme = TREE_SCHEME_FOR_FORMAT[format]
    if tree_size == 0:
        chain_hash = GENESIS_CHAIN_HASH
    else:
        entry = covered[tree_size - 1]
        chain_hash = entry.chain_hash

    return Checkpoint(
        tree_size=tree_size,
        merkle_root=root_for_scheme(leaves, scheme),
        chain_hash=chain_hash,
        timestamp=(timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc),
        origin=origin,
        previous_digest=previous_digest,
        format=format,
        key_id=key_id,
        entry_timestamps=entry_timestamps_root(covered) if format == FORMAT_C2SP_V3 else None,
    )



def sign_checkpoint(checkpoint: Checkpoint, signer: Signer) -> SignedCheckpoint:
    """Detach-sign a checkpoint.

    A note-format checkpoint stores the signature as a signed-note signature
    line would carry it: the 4-byte note key id (SHA-256 of key name, type byte
    and public key) followed by the raw Ed25519 signature. That makes the stored
    bytes exactly what a verifier needs, and it makes the formats visibly
    different -- a v1 signature is 32 bytes of HMAC or 64 of raw Ed25519, a note
    one is 68.

    A note checkpoint requires a signer that can produce a public key: the note
    key id is derived from it, so a signer without one is refused rather than
    quietly signing something unverifiable.

    v3 additionally requires the payload's key_id to match the signer and the
    signer's key_name to equal the checkpoint's origin (F8). Both are refusals,
    not warnings: a signature that does not name its own key inside the signed
    bytes can be replayed under a different key, and a note whose origin
    disagrees with the key name is a key signing for a log it was not issued
    for. DECISIONS.md says a key name SHOULD match the origin; here it MUST.
    """
    if checkpoint.format in NOTE_FORMATS:
        key_name = getattr(signer, "key_name", None) or checkpoint.origin
        public_key = _signer_public_key(signer)
        if not public_key:
            raise ValueError(
                f"a {checkpoint.format} checkpoint needs a signer exposing public_key_bytes() "
                "so the signed-note key id can be derived"
            )
        if checkpoint.format == FORMAT_C2SP_V3:
            if checkpoint.key_id is not None and checkpoint.key_id != signer.key_id:
                raise ValueError(
                    f"refusing to sign: the checkpoint's key_id {checkpoint.key_id!r} is not "
                    f"the signing key's {signer.key_id!r}"
                )
            if key_name != checkpoint.origin:
                raise ValueError(
                    f"refusing to sign: origin {checkpoint.origin!r} does not match the key "
                    f"name {key_name!r}. A key may only sign for the log it is named after."
                )
        raw = signer.sign(checkpoint.signing_bytes())
        key_id = note_key_id(key_name, public_key, signature_type=ED25519_NOTE_SIG_TYPE)
        return SignedCheckpoint(
            checkpoint=checkpoint,
            signature=key_id + raw,
            algorithm=signer.algorithm,
            key_id=signer.key_id,
            key_name=key_name,
        )
    return SignedCheckpoint(
        checkpoint=checkpoint,
        signature=signer.sign(checkpoint.signing_bytes()),
        algorithm=signer.algorithm,
        key_id=signer.key_id,
        key_name=getattr(signer, "key_name", None),
    )



def _signer_public_key(signer: Signer) -> bytes | None:
    """The signer's raw public key, or None when it cannot produce one."""
    getter = getattr(signer, "public_key_bytes", None)
    if not callable(getter):
        return None
    try:
        return bytes(getter())
    except Exception as exc:
        logger.info(f"Signer could not expose its public key: {type(exc).__name__}")
        return None


def verify_checkpoint(signed: SignedCheckpoint, signer: Signer) -> bool:
    """True only if the signature is intact for this key.

    Returns False rather than raising: a checkpoint is untrusted input, so a
    wrong algorithm, wrong key, truncated signature, or malformed hex is a
    verification failure and nothing more.

    For a note format four things are checked beyond the raw signature, and each
    closes a real hole:

    - the note key id in the stored signature is the one this public key
      derives, so a signature cannot be replayed under a different key name
    - the checkpoint's origin equals the signer's key name AND the key name in
      the note, so a key cannot sign a note claiming to be some other log (the
      spec says a key name SHOULD match the origin; here it is enforced,
      because the whole point of the origin is to say which log this is)
    - for v3, the key id inside the signed payload equals the verifying key's
      id, so the signature is bound to the key that made it
    - the algorithm matches, as before
    """
    if signed.algorithm != signer.algorithm or signed.key_id != signer.key_id:
        logger.info(
            f"Checkpoint key mismatch: signed by {signed.key_id}/{signed.algorithm}, "
            f"verified against {signer.key_id}/{signer.algorithm}"
        )
        return False
    checkpoint = signed.checkpoint
    if checkpoint.format in NOTE_FORMATS:
        key_name = signed.key_name or checkpoint.origin
        expected_name = getattr(signer, "key_name", None)
        if expected_name and key_name != expected_name:
            logger.info(
                f"Checkpoint key name {key_name!r} does not match the verifying "
                f"key's name {expected_name!r}"
            )
            return False
        if key_name != checkpoint.origin:
            # F8: a key may only sign for the log it is named after. A note
            # whose origin disagrees with its key name is a key signing for
            # some other log, and the origin is the only thing that says which.
            logger.info(
                f"Checkpoint origin {checkpoint.origin!r} does not match its key name "
                f"{key_name!r}"
            )
            return False
        if checkpoint.format == FORMAT_C2SP_V3 and checkpoint.key_id != signer.key_id:
            logger.info(
                f"Checkpoint payload names key {checkpoint.key_id!r} but is verified "
                f"against {signer.key_id!r}"
            )
            return False
        if len(signed.signature) <= KEY_ID_BYTES:
            logger.info("note checkpoint signature is too short to hold a note key id")
            return False
        if len(signed.signature) <= KEY_ID_BYTES:
            logger.info("v2 checkpoint signature is too short to carry a note key id")
            return False
        public_key = _signer_public_key(signer)
        if public_key:
            expected_id = note_key_id(key_name, public_key, signature_type=ED25519_NOTE_SIG_TYPE)
            if signed.signature[:KEY_ID_BYTES] != expected_id:
                logger.info(
                    f"Note key id {signed.signature[:KEY_ID_BYTES].hex()} does not match "
                    f"the id the verifying key derives ({expected_id.hex()})"
                )
                return False
        raw_signature = signed.signature[KEY_ID_BYTES:]
    else:
        raw_signature = signed.signature
    try:
        return signer.verify(signed.signing_bytes(), raw_signature)
    except Exception as exc:  # malformed signature bytes, wrong key type, ...
        logger.info(f"Checkpoint signature rejected: {type(exc).__name__}")
        return False


def checkpoint_digest(signed: SignedCheckpoint) -> str:
    """Hex digest handed to an anchor provider, and chained into the next checkpoint.

    sha256 over the signing bytes || signature. Anchoring this rather than the
    bare root means the anchored digest also commits to who signed and to the
    signature itself, and the next checkpoint carries it as previous_digest: a
    reader can then walk the checkpoint chain without the database.
    """
    material = signed.signing_bytes() + b"\n" + signed.signature
    return hashlib.sha256(material).hexdigest()


def signer_public_key(signer: Signer) -> bytes | None:
    """A signer's raw public key, or None. Public API: the key ceremony needs it."""
    return _signer_public_key(signer)