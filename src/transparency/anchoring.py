"""External timestamp anchoring -- the piece that makes the archive tamper-evident.

Signing a checkpoint proves the operator has not edited *that* checkpoint. It
does not stop the operator from publishing a different history next week and
signing it too. A hash chain in a database we control is only tamper-evident if
somebody we do not control holds a copy of the head -- so the head digest has to
leave the building and land somewhere with an independent clock. That is this
module, and it is the reason the checkpoint is signed at all: an anchored digest
that nobody can map back to a key we published is worthless.

What ships here is the interface and a stub. OpenTimestamps is the intended
provider because a Bitcoin timestamp costs nothing and cannot be un-asked, and
it returns an opaque `.ots` proof that a third party verifies without us.

Wiring a real attestation in later, with no change above this line:

1. Submit. Build the submit request the calendar expects -- POST the 32-byte
   SHA256 of the digest as a multipart form field named `submit`, alongside an
   `algorithm` field (SHA256) and any nonce extensions. Keep the digest in hex
   here and convert at the boundary; the provider should never touch canonical
   bytes.
2. Poll. The calendar answers with a Bitcoin transaction id, usually an
   unconfirmed coinbase containing our commitment. Poll until the calendar
   reports it has mined a timestamp, back off exponentially, and never block a
   pipeline run on it -- anchor asynchronously and let the receipt arrive later.
3. Fetch and verify the proof. Download the `.ots` proof and check it locally
   before trusting it: `ots verify` from opentimestamps-client, or the
   `opentimestamps` Python client, which re-derives the commitment, walks the
   Bitcoin merkle branch, and confirms the coinbase was timestamped after the
   block height we recorded. A receipt we cannot verify is a receipt we do not
   publish.
4. Store the receipt bytes. Put the returned proof into
   Attestation.receipt verbatim and persist the digest alongside it, so a third
   party gets both halves: the digest to check against our checkpoint, and the
   receipt to check against the calendar. Re-verify on a schedule -- if the
   calendar or the block chain reorgs away the timestamp, that is a finding, and
   the pipeline should surface it rather than silently re-anchor.
5. Publish the public key and the digests. Anchoring is only useful if the
   verifier path is public: ship the Ed25519 public key, the checkpoint, and the
   calendar URL in the repo or on a static page, so inclusion proofs at a
   permalink can be checked by a stranger.

Also deliberately absent: no client library import, no network call, no key
material, no retry policy. This stub is here so the rest of the package has a
shape to code against, not so anyone mistakes it for a timestamp.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from src.transparency.log import canonical_json

logger = logging.getLogger(__name__)

# An OpenTimestamps calendar. Free, and the calendar a real submission would
# POST to. Listed here as data so the stub can record intent without a
# dependency; nothing in this package makes a request to it.
OTS_CALENDAR_URL = "https://awoo.calendars.ots.net"


@dataclass(frozen=True)
class Attestation:
    """A third party's statement that it saw a digest at a point in time.

    `receipt` is opaque and unparsed on purpose: it is whatever the provider
    returns (for OTS, the raw `.ots` proof bytes) and this package must not
    depend on its internal format to hand it to a verifier.
    """
    provider: str
    digest_hex: str
    timestamp: datetime
    receipt: bytes


@runtime_checkable
class AnchorProvider(Protocol):
    """External witness for a checkpoint digest.

    async because a real provider does network I/O and the pipeline must not
    block an append on it.
    """

    name: str

    async def anchor(self, digest_hex: str) -> Attestation:
        """Get outside attestation that `digest_hex` existed. May be eventual:
        implementations may return a pending receipt and be called again."""
        ...


class OpenTimestampsStubProvider:
    """Records what it was asked to anchor. NOT A TIMESTAMP.

    No network call, no dependency on any OTS client, no receipt a third party
    could verify. It exists so the anchoring step in the pipeline and in
    scripts/run_transparency_demo.py has a real shape to exercise, and so the
    replacement can be a drop-in: implement AnchorProvider.anchor() for real
    calendar submission and nothing above this class changes.
    """

    name = "opentimestamps-stub"

    def __init__(self, calendar_url: str = OTS_CALENDAR_URL) -> None:
        self.calendar_url = calendar_url
        self._submitted: list[str] = []

    async def anchor(self, digest_hex: str) -> Attestation:
        if len(digest_hex) != 64 or not _is_hex(digest_hex):
            raise ValueError(f"Expected a 64-char hex SHA256 digest, got {digest_hex!r}")
        self._submitted.append(digest_hex)
        logger.info(
            f"[stub] would submit {digest_hex[:12]} to {self.calendar_url}; "
            "no timestamp is actually obtained"
        )
        receipt = canonical_json(
            {
                "calendar_url": self.calendar_url,
                "digest": digest_hex,
                "note": "NOT A TIMESTAMP: submission stubbed, no calendar was contacted",
                "provider": self.name,
                "stub": True,
            }
        )
        return Attestation(
            provider=self.name,
            digest_hex=digest_hex,
            timestamp=datetime.now(timezone.utc),
            receipt=receipt,
        )

    @property
    def submitted(self) -> list[str]:
        """Digests handed to anchor(), in order. Test and demo observability."""
        return list(self._submitted)


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True