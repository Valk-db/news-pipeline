"""Published operator keys: the trust roots the public proof page verifies against.

A checkpoint signature only means something against a key the reader can check
independently. This module loads the operator's *published* public keys from
the TRANSPARENCY_TRUSTED_KEYS environment variable (also exposed as the
`transparency_trusted_keys` setting) and hands the proof view a verifier for
the key a checkpoint claims to be signed by:

    {"<key_id>": {"algorithm": "ed25519", "public_key": "<64 hex chars>"}}

    verifier_for(signed, load_trusted_keys(raw)) -> Signer | None

Design rules, all deliberate:

- Only public-key algorithms are admitted to the trusted set. An HMAC entry
  is rejected with a warning: HmacDevSigner proves nothing to a third party
  (see checkpoint.py), so a page that "verified" against a server-held shared
  secret would be theater, not verification.
- A malformed config never fails closed in a way that hides data: bad entries
  are skipped loudly, which degrades proofs to the honest
  "unverified_signature" state rather than to a crash or a false "verified".
- Verification needs the optional `cryptography` package. When it is absent,
  verifier_for returns None and the page says the signature is unverified
  instead of pretending otherwise.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Mapping

from src.transparency.checkpoint import (
    ED25519_ALGORITHM,
    Ed25519Verifier,
    SignedCheckpoint,
    Signer,
    ed25519_available,
)

logger = logging.getLogger(__name__)

TRUSTED_KEYS_ENV_VAR = "TRANSPARENCY_TRUSTED_KEYS"

# Raw Ed25519 public keys are 32 bytes.
_ED25519_PUBLIC_KEY_BYTES = 32


@dataclass(frozen=True)
class TrustedKey:
    """One operator-published public key a checkpoint signature may verify against."""
    key_id: str
    algorithm: str
    public_key: bytes  # raw public key bytes (32 bytes for Ed25519)


def load_trusted_keys(raw: str | None) -> dict[str, TrustedKey]:
    """Parse the TRANSPARENCY_TRUSTED_KEYS JSON into key_id -> TrustedKey.

    Invalid JSON, a non-object document, and malformed entries are skipped
    with a warning: each skipped entry only shrinks the trusted set, which
    degrades proofs to "unverified_signature" (the safe direction), never to
    a false "verified".
    """
    trusted: dict[str, TrustedKey] = {}
    if not raw or not raw.strip():
        return trusted
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("%s is not valid JSON: %s", TRUSTED_KEYS_ENV_VAR, exc)
        return trusted
    if not isinstance(data, dict):
        logger.warning("%s must be a JSON object mapping key_id to key spec", TRUSTED_KEYS_ENV_VAR)
        return trusted
    for key_id, spec in data.items():
        if not isinstance(key_id, str) or not key_id:
            logger.warning("Skipping trusted-key entry with non-string key id %r", key_id)
            continue
        if not isinstance(spec, dict):
            logger.warning("Skipping trusted key %r: spec must be an object", key_id)
            continue
        algorithm = spec.get("algorithm")
        if algorithm != ED25519_ALGORITHM:
            # Deliberate: only public-key algorithms are publicly verifiable.
            # An HMAC "verifies" only for whoever holds the shared secret, so
            # admitting one here would let the page claim third-party
            # verifiability it does not have.
            logger.warning(
                "Skipping trusted key %r: algorithm %r is not publicly verifiable "
                "(only %r is admitted)",
                key_id, algorithm, ED25519_ALGORITHM,
            )
            continue
        public_key_hex = spec.get("public_key")
        try:
            public_key = bytes.fromhex(str(public_key_hex))
        except (TypeError, ValueError):
            logger.warning("Skipping trusted key %r: public_key is not valid hex", key_id)
            continue
        if len(public_key) != _ED25519_PUBLIC_KEY_BYTES:
            logger.warning(
                "Skipping trusted key %r: ed25519 public key must be %d bytes, got %d",
                key_id, _ED25519_PUBLIC_KEY_BYTES, len(public_key),
            )
            continue
        trusted[key_id] = TrustedKey(key_id=key_id, algorithm=algorithm, public_key=public_key)
    return trusted


def verifier_for(signed: SignedCheckpoint, trusted: Mapping[str, TrustedKey]) -> Signer | None:
    """Return a public-key-only verifier for the key that signed this checkpoint.

    None means the signature cannot be checked against a published key: the
    key id is unknown, the algorithms disagree, or the `cryptography` package
    is unavailable. The caller must render the honest unverified state, never
    treat None as a pass.
    """
    key = trusted.get(signed.key_id)
    if key is None:
        logger.info("No published key for checkpoint key id %r", signed.key_id)
        return None
    if key.algorithm != signed.algorithm:
        logger.info(
            "Checkpoint key id %r: signed with %r, published key is %r",
            signed.key_id, signed.algorithm, key.algorithm,
        )
        return None
    if key.algorithm != ED25519_ALGORITHM:  # defensive: load_trusted_keys already filters
        return None
    if not ed25519_available():
        logger.warning(
            "Cannot verify checkpoint signature for key %r: "
            "the 'cryptography' package is not installed",
            signed.key_id,
        )
        return None
    try:
        return Ed25519Verifier(key.public_key, key_id=key.key_id)
    except Exception as exc:
        logger.warning("Published key %r is unusable: %s", signed.key_id, exc)
        return None
