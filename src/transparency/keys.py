"""Published operator keys: the trust roots the public proof page verifies against.

A checkpoint signature only means something against a key the reader can check
independently. This module loads the operator's *published* public keys from
the TRANSPARENCY_TRUSTED_KEYS environment variable (also exposed as the
`transparency_trusted_keys` setting) and hands the proof view a verifier for
the key a checkpoint claims to be signed by:

    {"<key_id>": {"algorithm": "ed25519", "public_key": "<64 hex chars>"}}

    verifier_for(signed, load_trusted_keys(raw)) -> Signer | None

Each entry may also carry validity bounds, which is what stops a leaked old key
from being able to sign a fresh history or an old key from vouching for
checkpoints from after it was supposed to be dead:

    {"<key_id>": {"algorithm": "ed25519", "public_key": "...",
                  "key_name": "procmon.dev/transparency",
                  "not_before": "2026-10-02T00:00:00Z",
                  "not_after": "2027-10-02T00:00:00Z",
                  "max_tree_size": 1000000}}

not_before/not_after bound the checkpoint *timestamp* (when the operator says
they signed) and max_tree_size bounds the size the checkpoint covers. All three
are optional but a key with none of them is logged as unbounded: rotation only
means something if the old key stops working at some point, and the way to
guarantee that is a bound somebody wrote down.

Design rules, all deliberate:

- Only public-key algorithms are admitted to the trusted set. An HMAC entry
  is rejected with a warning: HmacDevSigner proves nothing to a third party
  (see checkpoint.py), so a page that "verified" against a server-held shared
  secret would be theater, not verification.
- A malformed config never fails closed in a way that hides data: a bad entry is
  either skipped loudly (degrading that key to the honest "unverified_signature"
  state) or, for anything wrong with a specific key entry including its validity
  bounds, raises TrustedKeyError so the caller refuses outright. A key is never
  admitted with a bound quietly nulled out -- that is the F6 defect, and it
  turned a typo into a key that never expires.
- Verification needs the optional `cryptography` package. When it is absent,
  verifier_for returns None and the page says the signature is unverified
  instead of pretending otherwise.
- Bounds are enforced, not decorative. A checkpoint outside its key's validity
  window is not verifiable, which degrades to the honest unverified state.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, UTC
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
    """One operator-published public key a checkpoint signature may verify against.

    The three bound fields are all optional and default to None, which means
    unbounded -- a key that never expires and could vouch for a checkpoint at
    any tree size. That is the state a first key starts in; load_trusted_keys
    warns about it so the gap is visible rather than assumed away.
    """
    key_id: str
    algorithm: str
    public_key: bytes  # raw public key bytes (32 bytes for Ed25519)
    key_name: str | None = None       # C2SP signed-note key name
    not_before: datetime | None = None
    not_after: datetime | None = None
    max_tree_size: int | None = None

    def is_within_bounds(self, tree_size: int, timestamp: datetime) -> tuple[bool, str]:
        """(ok, reason) for a checkpoint at this size and time.

        Timestamps compare as aware datetimes; a checkpoint timestamp read back
        from the database can be naive if a session hands back a naive value, so
        a naive one is treated as UTC rather than raising.
        """
        moment = _as_utc(timestamp)
        if self.not_before is not None and moment < _as_utc(self.not_before):
            return False, f"checkpoint is older than the key's not_before ({self.not_before.isoformat()})"
        if self.not_after is not None and moment > _as_utc(self.not_after):
            return False, f"checkpoint is newer than the key's not_after ({self.not_after.isoformat()})"
        if self.max_tree_size is not None and tree_size > self.max_tree_size:
            return False, f"checkpoint tree_size {tree_size} exceeds the key's max_tree_size {self.max_tree_size}"
        return True, "within bounds"

    def has_bounds(self) -> bool:
        return self.not_before is not None or self.not_after is not None or self.max_tree_size is not None


def _as_utc(moment: datetime) -> datetime:
    """Aware UTC datetime, assuming UTC when the value came back naive."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class TrustedKeyError(ValueError):
    """A trust store that cannot be loaded safely (F6).

    Raised instead of quietly dropping a bound. A malformed `not_after` used to
    parse to None, which is not "no bound" but "the bound the operator wrote,
    ignored" -- and an unbounded key verifies forever, which is the exact failure
    key validity bounds exist to prevent. Failing the whole load is the
    fail-closed direction: no store means nothing verifies, and the operator
    sees an error naming the key and the field.
    """


def _parse_timestamp(key_id: str, field: str, value: object) -> datetime | None:
    """Parse an ISO-8601 bound. None means "field absent"; unparseable RAISES.

    The distinction is the whole point of F6: absent is unbounded on purpose,
    unparseable is a mistake in the trust store, and treating the second like
    the first is how a key that should have expired stays trusted.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as exc:
        raise TrustedKeyError(
            f"trusted key {key_id!r}: {field}={value!r} is not an ISO-8601 timestamp"
        ) from exc


def _parse_max_tree_size(key_id: str, value: object) -> int | None:
    """Parse a max_tree_size bound. None means absent; unparseable RAISES (F6)."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TrustedKeyError(
            f"trusted key {key_id!r}: max_tree_size={value!r} is not an integer"
        ) from exc
    if parsed < 0:
        raise TrustedKeyError(f"trusted key {key_id!r}: max_tree_size must be >= 0, got {parsed}")
    return parsed


def load_trusted_keys(raw: str | None) -> dict[str, TrustedKey]:
    """Parse the TRANSPARENCY_TRUSTED_KEYS JSON into key_id -> TrustedKey.

    Entries that cannot be *admitted* (unknown algorithm, bad hex, wrong key
    length) are skipped with a warning: each skipped entry only shrinks the
    trusted set, which degrades proofs to "unverified_signature" (the safe
    direction), never to a false "verified".

    A malformed *validity bound* -- and anything else wrong with a specific key
    entry -- is different and raises TrustedKeyError (F6). A bound that cannot
    be parsed is not a key without a bound, it is a key whose expiry the
    operator believes they set and the loader threw away, and an unbounded key
    never expires. Rather than null the bound and keep the key, the whole load
    fails: nothing verifies until the trust store is fixed, which is the
    direction that cannot be exploited by a typo. Callers that must keep running
    should catch TrustedKeyError and refuse to sign, not fall back to a
    partially parsed store.

    Document-level failures (unparseable JSON, a document that is not an object)
    still return an empty store: no key is admitted, so nothing verifies, which
    is the same fail-closed outcome without turning a typo in a config value
    into an exception on the proof page's request path.
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
            raise TrustedKeyError(f"{TRUSTED_KEYS_ENV_VAR} has an entry with a non-string key id {key_id!r}")
        if not isinstance(spec, dict):
            raise TrustedKeyError(f"trusted key {key_id!r}: spec must be an object")
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
            raise TrustedKeyError(f"trusted key {key_id!r}: public_key is not valid hex") from None
        if len(public_key) != _ED25519_PUBLIC_KEY_BYTES:
            raise TrustedKeyError(
                f"trusted key {key_id!r}: ed25519 public key must be "
                f"{_ED25519_PUBLIC_KEY_BYTES} bytes, got {len(public_key)}"
            )
        not_before = _parse_timestamp(key_id, "not_before", spec.get("not_before"))
        not_after = _parse_timestamp(key_id, "not_after", spec.get("not_after"))
        max_tree_size = _parse_max_tree_size(key_id, spec.get("max_tree_size"))
        if not_before is not None and not_after is not None and not_before > not_after:
            raise TrustedKeyError(
                f"trusted key {key_id!r}: not_before is later than not_after, so no "
                "checkpoint could ever verify"
            )
        key_name = spec.get("key_name")
        if key_name is not None and not isinstance(key_name, str):
            raise TrustedKeyError(f"trusted key {key_id!r}: key_name must be a string")
        key = TrustedKey(
            key_id=key_id,
            algorithm=algorithm,
            public_key=public_key,
            key_name=key_name or None,
            not_before=not_before,
            not_after=not_after,
            max_tree_size=max_tree_size,
        )
        if not key.has_bounds():
            logger.warning(
                "Trusted key %r has no validity bounds (not_before/not_after/max_tree_size). "
                "It will verify checkpoints at any time and any tree size, so rotating it "
                "later requires removing it from %s.",
                key_id, TRUSTED_KEYS_ENV_VAR,
            )
        trusted[key_id] = key
    return trusted



def _public_key_bytes(key: TrustedKey) -> bytes:
    """The key's public key as raw bytes, accepting the hex form too.

    TrustedKey.public_key is typed bytes and load_trusted_keys always produces
    bytes. But a hand-built TrustedKey holding the 64-char hex string -- which is
    what TRANSPARENCY_TRUSTED_KEYS looks like in every config file and in every
    operator's notes -- would otherwise reach Ed25519Verifier, raise, and be
    swallowed into the honest-unverified state. That failure is invisible on the
    page: the proof renders as "signature unverified" for a key that is actually
    fine, which is exactly the kind of quiet wrongness this module exists to
    avoid. So the hex form is accepted here rather than turned into a badge that
    lies.
    """
    if isinstance(key.public_key, (bytes, bytearray)):
        return bytes(key.public_key)
    if isinstance(key.public_key, str):
        try:
            return bytes.fromhex(key.public_key)
        except ValueError:
            return key.public_key.encode("utf-8", "replace")
    return bytes(key.public_key)


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
    # Bounds are read off the checkpoint rather than taken as arguments, so
    # there is no way to verify a signature without them. getattr rather than
    # attribute access: this function is duck-typed by its callers and by its
    # tests, and a stand-in that carries no checkpoint has nothing to bound,
    # which the key's own is_within_bounds agrees with (no bounds set, no
    # rejection).
    checkpoint = getattr(signed, "checkpoint", None)
    tree_size = getattr(checkpoint, "tree_size", None)
    timestamp = getattr(checkpoint, "timestamp", None)
    if tree_size is not None and timestamp is not None:
        ok, reason = key.is_within_bounds(tree_size, timestamp)
        if not ok:
            logger.info("Trusted key %r is not valid for this checkpoint: %s", key.key_id, reason)
            return None
    signed_key_name = getattr(signed, "key_name", None)
    if key.key_name and signed_key_name and key.key_name != signed_key_name:
        logger.info(
            "Trusted key %r publishes key name %r but the checkpoint is signed as %r",
            key.key_id, key.key_name, signed.key_name,
        )
        return None
    try:
        return Ed25519Verifier(_public_key_bytes(key), key_id=key.key_id, key_name=key.key_name)
    except Exception as exc:
        logger.warning("Published key %r is unusable: %s", signed.key_id, exc)
        return None
