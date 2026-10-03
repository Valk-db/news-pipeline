"""Tests for the published-operator-key trust roots (S-P1-1).

Covers config parsing (valid keys admitted, HMAC/non-public-key algorithms
rejected, malformed entries skipped loudly) and verifier lookup (unknown key
id, algorithm mismatch, and missing `cryptography` package all yield None,
never a false pass).
"""
import hashlib
import json

import pytest

from src.transparency.checkpoint import ED25519_ALGORITHM, ed25519_available
from src.transparency.keys import TRUSTED_KEYS_ENV_VAR, TrustedKey, load_trusted_keys, verifier_for


def _seed(label: str) -> bytes:
    """A deterministic >= 32-byte signing seed for a test.

    generate_ed25519_signer refuses a seed shorter than MIN_SIGNING_SEED_BYTES,
    so tests that need a reproducible key pair cannot use a short label. This
    hashes the label to exactly 32 bytes, which is deterministic per label and
    satisfies the production constraint rather than bypassing it.
    """
    return hashlib.sha256(f"test-seed:{label}".encode()).digest()



def _raw(entries):
    return json.dumps(entries)


def _ed_entry(key_id="k1"):
    return {key_id: {"algorithm": ED25519_ALGORITHM, "public_key": "ab" * 32}}


class TestLoadTrustedKeys:
    def test_empty_config_yields_no_keys(self):
        assert load_trusted_keys("") == {}
        assert load_trusted_keys(None) == {}
        assert load_trusted_keys("   ") == {}

    def test_valid_ed25519_key_is_admitted(self):
        trusted = load_trusted_keys(_raw(_ed_entry("op-2026")))
        assert set(trusted) == {"op-2026"}
        key = trusted["op-2026"]
        assert isinstance(key, TrustedKey)
        assert key.algorithm == ED25519_ALGORITHM
        assert key.public_key == bytes.fromhex("ab" * 32)

    def test_hmac_entry_is_rejected(self, caplog):
        """An HMAC proves nothing to a third party: admitting it would let the
        page claim public verifiability it does not have."""
        trusted = load_trusted_keys(_raw({
            "dev": {"algorithm": "hmac-sha256-dev", "public_key": "ab" * 32},
        }))
        assert trusted == {}
        assert "not publicly verifiable" in caplog.text

    def test_malformed_entries_are_skipped_not_fatal(self, caplog):
        trusted = load_trusted_keys(_raw({
            "good": {"algorithm": ED25519_ALGORITHM, "public_key": "cd" * 32},
            "bad-hex": {"algorithm": ED25519_ALGORITHM, "public_key": "zz"},
            "short": {"algorithm": ED25519_ALGORITHM, "public_key": "ab" * 16},
            "no-spec": "not-an-object",
            "": {"algorithm": ED25519_ALGORITHM, "public_key": "ab" * 32},
        }))
        assert set(trusted) == {"good"}

    def test_invalid_json_yields_no_keys(self, caplog):
        assert load_trusted_keys("{not json") == {}
        assert load_trusted_keys(_raw(["a", "list"])) == {}


class FakeSigned:
    def __init__(self, key_id="k1", algorithm=ED25519_ALGORITHM):
        self.key_id = key_id
        self.algorithm = algorithm


class TestVerifierFor:
    def test_unknown_key_id_yields_none(self):
        assert verifier_for(FakeSigned("nope"), {"k1": TrustedKey("k1", ED25519_ALGORITHM, b"x" * 32)}) is None

    def test_algorithm_mismatch_yields_none(self):
        trusted = {"k1": TrustedKey("k1", ED25519_ALGORITHM, b"x" * 32)}
        assert verifier_for(FakeSigned("k1", "hmac-sha256-dev"), trusted) is None

    def test_empty_trusted_set_yields_none(self):
        assert verifier_for(FakeSigned("k1"), {}) is None

    @pytest.mark.skipif(not ed25519_available(), reason="cryptography is not installed")
    def test_known_ed25519_key_yields_verifier(self):
        from src.transparency.checkpoint import generate_ed25519_signer

        signer = generate_ed25519_signer(seed=_seed("keys-test"))
        trusted = {
            signer.key_id: TrustedKey(signer.key_id, ED25519_ALGORITHM, signer.public_key_bytes())
        }
        verifier = verifier_for(FakeSigned(signer.key_id), trusted)
        assert verifier is not None
        assert verifier.key_id == signer.key_id
        # And the verifier actually verifies what the signer signed.
        message = b"checkpoint bytes"
        assert verifier.verify(message, signer.sign(message))
        assert not verifier.verify(b"other bytes", signer.sign(message))

    def test_missing_cryptography_yields_none(self, monkeypatch):
        import src.transparency.keys as keys_module

        monkeypatch.setattr(keys_module, "ed25519_available", lambda: False)
        trusted = {"k1": TrustedKey("k1", ED25519_ALGORITHM, b"x" * 32)}
        assert verifier_for(FakeSigned("k1"), trusted) is None

    def test_env_var_name_is_stable(self):
        assert TRUSTED_KEYS_ENV_VAR == "TRANSPARENCY_TRUSTED_KEYS"
