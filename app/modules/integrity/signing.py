"""Local Ed25519 checkpoint signing and verification.

Private key material is sourced only from `Settings.integrity_signing_key`
(environment/config) and is never logged, returned, included in an error
message, or committed. No external KMS/HSM -- see
`docs/runbooks/local-development.md` for the dev-only key-generation
workflow and `docs/decisions/ADR-013-phase-6-integrity-checkpoints.md` for
why this is a local signature, not a blockchain anchor.

A signed checkpoint's public key and its fingerprint are stored alongside
the signature (`CheckpointSignatureRecord`) -- a public key is not secret,
and storing it there means `verify()` needs no access to `Settings` or the
private key at all, satisfying "verification must work from public
verification material alone".
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.config import Settings
from app.modules.integrity.models import SIGNATURE_ALGORITHM, SIGNATURE_ENCODING

#: Matches `access_control.audit.hash_ip`'s truncated-marker convention --
#: long enough to distinguish keys in practice, short enough to read as an
#: identifier rather than a reconstructable key.
_FINGERPRINT_LENGTH = 16

_PRIVATE_KEY_RAW_LENGTH = 32


class SigningKeyNotConfiguredError(RuntimeError):
    """Raised when a checkpoint-signing operation is attempted with no configured key."""


class SigningKeyInvalidError(RuntimeError):
    """Raised when the configured key material is not a valid Ed25519 private key."""


@dataclass(frozen=True)
class SignedRoot:
    key_id: str
    algorithm: str
    signature_encoding: str
    signature_b64: str
    public_key_b64: str
    public_key_fingerprint: str


def public_key_fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:_FINGERPRINT_LENGTH]


class LoadedSigningKey:
    """A private key held only in memory for the duration of one sign call."""

    def __init__(self, *, key_id: str, private_key: Ed25519PrivateKey) -> None:
        self._key_id = key_id
        self._private_key = private_key

    def sign(self, root_hash_hex: str) -> SignedRoot:
        signature = self._private_key.sign(bytes.fromhex(root_hash_hex))
        public_key = self._private_key.public_key()
        raw_public = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return SignedRoot(
            key_id=self._key_id,
            algorithm=SIGNATURE_ALGORITHM,
            signature_encoding=SIGNATURE_ENCODING,
            signature_b64=base64.b64encode(signature).decode("ascii"),
            public_key_b64=base64.b64encode(raw_public).decode("ascii"),
            public_key_fingerprint=public_key_fingerprint(public_key),
        )


def load_signing_key(settings: Settings) -> LoadedSigningKey:
    """Load the configured Ed25519 signing key. Never logs or echoes the key."""
    if settings.integrity_signing_key is None:
        raise SigningKeyNotConfiguredError("INTEGRITY_SIGNING_KEY is not configured")
    try:
        raw = base64.b64decode(settings.integrity_signing_key.get_secret_value(), validate=True)
    except Exception as exc:
        raise SigningKeyInvalidError("INTEGRITY_SIGNING_KEY is not valid base64") from exc
    if len(raw) != _PRIVATE_KEY_RAW_LENGTH:
        raise SigningKeyInvalidError(
            f"INTEGRITY_SIGNING_KEY must decode to {_PRIVATE_KEY_RAW_LENGTH} raw bytes"
        )
    private_key = Ed25519PrivateKey.from_private_bytes(raw)
    return LoadedSigningKey(key_id=settings.integrity_signing_key_id, private_key=private_key)


def generate_signing_key_b64() -> str:
    """Generate a new random Ed25519 private key, base64-encoded.

    Dev-only convenience for `cli.py generate-key`. The caller is
    responsible for placing the result in a local, git-ignored `.env` --
    this function never writes to disk or logs the value.
    """
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    return base64.b64encode(raw).decode("ascii")


def verify(*, root_hash_hex: str, signature_b64: str, public_key_b64: str) -> bool:
    """Verify a checkpoint-root signature from public material alone.

    Returns `False` (never raises) for any malformed input, wrong key, or
    altered root/signature -- callers get a clear boolean, not an exception
    that might leak internals.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        signature = base64.b64decode(signature_b64)
        root_bytes = bytes.fromhex(root_hash_hex)
    except Exception:
        return False
    try:
        public_key.verify(signature, root_bytes)
        return True
    except InvalidSignature:
        return False
