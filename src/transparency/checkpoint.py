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
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from src.transparency.log import GENESIS_CHAIN_HASH, MerkleLog, canonical_json

logger = logging.getLogger(__name__)

# Domain separator so a checkpoint signature can never be replayed as a
# signature over some other document this project might start signing.
CHECKPOINT_DOMAIN = b"n1:merkle-checkpoint:v1"

# Internal Merkle nodes are prefixed; leaves are not, because log.py fixes
# leaf_hash = sha256(canonical payload) with no prefix.
NODE_PREFIX = b"\x01"

# Root of a zero-leaf tree. Only reachable for an empty log, which should never
# be signed, but the function must be total.
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


def merkle_levels(leaf_hashes: Sequence[bytes]) -> list[list[bytes]]:
    """Build the tree bottom-up, duplicating the last node of an odd level.

    This is the Certificate Transparency tree: an odd level repeats its final
    node instead of promoting it. merkle_root() and proofs.py both go through
    this function so a root and a proof can never disagree about the shape.
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
    """Root over the given leaf hashes, or EMPTY_TREE_ROOT for none."""
    levels = merkle_levels(leaf_hashes)
    return levels[-1][0] if levels else EMPTY_TREE_ROOT


def tree_depth(tree_size: int) -> int:
    """Number of internal nodes on a root-to-leaf path: ceil(log2(size))."""
    depth = 0
    span = 1
    while span < tree_size:
        span *= 2
        depth += 1
    return depth


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

    def __init__(self, private_key: bytes, *, key_id: str | None = None) -> None:
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

    def __init__(self, public_key: bytes, *, key_id: str = "ed25519") -> None:
        if not _ED25519_AVAILABLE:
            raise RuntimeError("Ed25519Verifier needs the 'cryptography' package.")
        self._public_key = Ed25519PublicKey.from_public_bytes(public_key)
        self.key_id = key_id

    def sign(self, message: bytes) -> bytes:
        raise PermissionError("Ed25519Verifier holds no private key and cannot sign.")

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self._public_key.verify(signature, message)
        except Exception:  # InvalidSignature and malformed input both mean "no"
            return False
        return True


def generate_ed25519_signer(seed: bytes | None = None) -> Ed25519Signer:
    """Fresh Ed25519 keypair.

    `seed` is arbitrary-length seed material (a passphrase is fine): it is
    hashed down to the 32 bytes Ed25519 requires, so a demo key is reproducible
    without callers counting bytes. Omit it for a random key -- and in
    production, generate the key outside this process and never store the seed
    next to the log.
    """
    if not _ED25519_AVAILABLE:
        raise RuntimeError("generate_ed25519_signer needs the 'cryptography' package.")
    if seed is None:
        key = Ed25519PrivateKey.generate()
    else:
        key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed).digest())
    return Ed25519Signer(bytes(key.private_bytes_raw()))


@dataclass(frozen=True)
class Checkpoint:
    """The signed body. Frozen: nothing may change between signing and anchoring."""
    tree_size: int
    merkle_root: bytes
    chain_hash: bytes
    timestamp: datetime

    def signing_bytes(self) -> bytes:
        """Domain-separated canonical bytes that get signed.

        Independent of the signature format, so any verifier can reproduce it
        from the published fields.
        """
        body = {
            "chain_hash": self.chain_hash.hex(),
            "hash_scheme": "n1:sha256",
            "merkle_root": self.merkle_root.hex(),
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tree_size": self.tree_size,
        }
        return CHECKPOINT_DOMAIN + b" " + canonical_json(body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_hash": self.chain_hash.hex(),
            "merkle_root": self.merkle_root.hex(),
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tree_size": self.tree_size,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Checkpoint:
        return cls(
            tree_size=int(data["tree_size"]),
            merkle_root=bytes.fromhex(str(data["merkle_root"])),
            chain_hash=bytes.fromhex(str(data["chain_hash"])),
            timestamp=datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00")),
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

    def signing_bytes(self) -> bytes:
        return self.checkpoint.signing_bytes()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checkpoint.to_dict(),
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": self.signature.hex(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SignedCheckpoint:
        return cls(
            checkpoint=Checkpoint.from_dict(data),
            signature=bytes.fromhex(str(data["signature"])),
            algorithm=str(data["algorithm"]),
            key_id=str(data["key_id"]),
        )


async def build_checkpoint(
    log: MerkleLog,
    tree_size: int,
    *,
    timestamp: datetime | None = None,
) -> Checkpoint:
    """Checkpoint the first `tree_size` entries. `tree_size` may be smaller than
    the log, which is how a mid-flight checkpoint stays verifiable."""
    if tree_size < 0:
        raise ValueError(f"tree_size must be >= 0, got {tree_size}")
    available = await log.size()
    if tree_size > available:
        raise ValueError(f"tree_size {tree_size} exceeds log size {available}")

    leaves = (await log.leaf_hashes())[:tree_size]
    if tree_size == 0:
        chain_hash = GENESIS_CHAIN_HASH
    else:
        entry = await log.get(tree_size - 1)
        assert entry is not None  # guaranteed by the size check above
        chain_hash = entry.chain_hash

    return Checkpoint(
        tree_size=tree_size,
        merkle_root=merkle_root(leaves),
        chain_hash=chain_hash,
        timestamp=(timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc),
    )


def sign_checkpoint(checkpoint: Checkpoint, signer: Signer) -> SignedCheckpoint:
    """Detach-sign a checkpoint."""
    return SignedCheckpoint(
        checkpoint=checkpoint,
        signature=signer.sign(checkpoint.signing_bytes()),
        algorithm=signer.algorithm,
        key_id=signer.key_id,
    )


def verify_checkpoint(signed: SignedCheckpoint, signer: Signer) -> bool:
    """True only if the signature is intact for this key.

    Returns False rather than raising: a checkpoint is untrusted input, so a
    wrong algorithm, wrong key, truncated signature, or malformed hex is a
    verification failure and nothing more.
    """
    if signed.algorithm != signer.algorithm or signed.key_id != signer.key_id:
        logger.info(
            f"Checkpoint key mismatch: signed by {signed.key_id}/{signed.algorithm}, "
            f"verified against {signer.key_id}/{signer.algorithm}"
        )
        return False
    try:
        return signer.verify(signed.signing_bytes(), signed.signature)
    except Exception as exc:  # malformed signature bytes, wrong key type, ...
        logger.info(f"Checkpoint signature rejected: {type(exc).__name__}")
        return False


def checkpoint_digest(signed: SignedCheckpoint) -> str:
    """Hex digest handed to an anchor provider: sha256 over signing bytes || signature.

    Anchoring this rather than the bare root means the anchored digest also
    commits to who signed and to the signature itself.
    """
    material = signed.signing_bytes() + b"\n" + signed.signature
    return hashlib.sha256(material).hexdigest()