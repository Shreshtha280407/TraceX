"""Password hashing and session tokens — stdlib only, no new dependency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from app.config import settings

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = password_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(computed.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_session_token(subject: str) -> str:
    payload = json.dumps({"sub": subject, "exp": time.time() + settings.token_ttl_seconds}, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def verify_session_token(token: str) -> str | None:
    try:
        payload_part, signature_part = token.split(".")
        payload = _b64decode(payload_part)
        signature = _b64decode(signature_part)
    except Exception:  # noqa: BLE001 — any malformed token is simply invalid
        return None
    expected = hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if claims.get("exp", 0) < time.time():
        return None
    subject = claims.get("sub")
    return subject if isinstance(subject, str) else None
