"""Ed25519 signing/verification coverage: proof points 14, 15, and 16 (key material).

Local, in-memory only -- no PostgreSQL needed for any test in this file.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import Settings
from app.modules.integrity.signing import (
    LoadedSigningKey,
    SigningKeyInvalidError,
    SigningKeyNotConfiguredError,
    generate_signing_key_b64,
    load_signing_key,
    public_key_fingerprint,
    verify,
)

_ROOT_HASH = "a" * 64
_OTHER_ROOT_HASH = "b" * 64


def _loaded_key(key_id: str = "test-key-1") -> LoadedSigningKey:
    private_key = Ed25519PrivateKey.generate()
    return LoadedSigningKey(key_id=key_id, private_key=private_key)


def test_valid_signature_verifies() -> None:
    signed = _loaded_key().sign(_ROOT_HASH)
    assert verify(
        root_hash_hex=_ROOT_HASH,
        signature_b64=signed.signature_b64,
        public_key_b64=signed.public_key_b64,
    )


def test_wrong_public_key_fails_verification() -> None:
    signed = _loaded_key().sign(_ROOT_HASH)
    other_signed = _loaded_key(key_id="other-key").sign(_ROOT_HASH)
    assert not verify(
        root_hash_hex=_ROOT_HASH,
        signature_b64=signed.signature_b64,
        public_key_b64=other_signed.public_key_b64,
    )


def test_altered_signature_fails_verification() -> None:
    signed = _loaded_key().sign(_ROOT_HASH)
    tampered_bytes = bytearray(base64.b64decode(signed.signature_b64))
    tampered_bytes[0] ^= 0xFF
    tampered_signature_b64 = base64.b64encode(bytes(tampered_bytes)).decode("ascii")
    assert not verify(
        root_hash_hex=_ROOT_HASH,
        signature_b64=tampered_signature_b64,
        public_key_b64=signed.public_key_b64,
    )


def test_altered_root_fails_verification() -> None:
    """A signature over one root must not verify against a different root."""
    signed = _loaded_key().sign(_ROOT_HASH)
    assert not verify(
        root_hash_hex=_OTHER_ROOT_HASH,
        signature_b64=signed.signature_b64,
        public_key_b64=signed.public_key_b64,
    )


def test_malformed_verification_input_fails_closed_not_raises() -> None:
    assert not verify(root_hash_hex="not-hex", signature_b64="!!!", public_key_b64="!!!")


def test_signed_root_never_carries_private_key_material() -> None:
    """Proof point 16: a `SignedRoot` exposes only public, storable fields."""
    signed = _loaded_key().sign(_ROOT_HASH)
    public_fields = vars(signed)
    assert set(public_fields) == {
        "key_id",
        "algorithm",
        "signature_encoding",
        "signature_b64",
        "public_key_b64",
        "public_key_fingerprint",
    }
    # The public key round-trips to a 32-byte raw Ed25519 public key -- never
    # a 32-byte *private* key or any larger private-key-shaped structure.
    assert len(base64.b64decode(signed.public_key_b64)) == 32


def test_fingerprint_is_stable_and_shorter_than_the_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    fingerprint_a = public_key_fingerprint(private_key.public_key())
    fingerprint_b = public_key_fingerprint(private_key.public_key())
    assert fingerprint_a == fingerprint_b
    assert len(fingerprint_a) < 64


def test_generate_signing_key_produces_a_valid_32_byte_key() -> None:
    key_b64 = generate_signing_key_b64()
    raw = base64.b64decode(key_b64, validate=True)
    assert len(raw) == 32
    # Round-trips through the real loader shape (private_bytes -> key object).
    Ed25519PrivateKey.from_private_bytes(raw)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]


def test_load_signing_key_raises_a_clear_error_when_unconfigured() -> None:
    with pytest.raises(SigningKeyNotConfiguredError):
        load_signing_key(_settings(integrity_signing_key=None))


def test_load_signing_key_rejects_malformed_base64() -> None:
    with pytest.raises(SigningKeyInvalidError):
        load_signing_key(_settings(integrity_signing_key="not-valid-base64!!!"))


def test_load_signing_key_rejects_wrong_length_key() -> None:
    too_short = base64.b64encode(b"short").decode("ascii")
    with pytest.raises(SigningKeyInvalidError):
        load_signing_key(_settings(integrity_signing_key=too_short))


def test_load_signing_key_round_trips_a_generated_key() -> None:
    key_b64 = generate_signing_key_b64()
    loaded = load_signing_key(
        _settings(integrity_signing_key=key_b64, integrity_signing_key_id="my-key-1")
    )
    signed = loaded.sign(_ROOT_HASH)
    assert signed.key_id == "my-key-1"
    assert verify(
        root_hash_hex=_ROOT_HASH,
        signature_b64=signed.signature_b64,
        public_key_b64=signed.public_key_b64,
    )


def test_blank_signing_key_env_value_normalizes_to_not_configured() -> None:
    """The exact Phase 2.1 `docker compose` blank-substitution bug, now also covering this field."""
    with pytest.raises(SigningKeyNotConfiguredError):
        load_signing_key(_settings(integrity_signing_key="   "))
