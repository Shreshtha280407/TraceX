"""TOTP (RFC 6238) generation and verification, and admin-forced credential ceremony support.

Hand-rolled against the RFC rather than adding a third-party dependency:
`password.py`'s own docstring documents this module's deliberately narrow,
reviewed dependency list (only PyJWT and pwdlib[argon2]), and RFC 6238 over
HMAC-SHA1 is small enough (~30 lines) that reimplementing it correctly is
cheaper than auditing a new supply-chain dependency for it. Parameters
(SHA1, 6 digits, 30-second step) match the Google Authenticator / RFC 6238
defaults every real authenticator app already expects -- this is not a
custom scheme.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

#: RFC 4226 recommends >= 128 bits; 160 bits (20 bytes) matches the
#: HMAC-SHA1 block size and is what most authenticator apps expect.
_SECRET_BYTES = 20
_DIGITS = 6
_PERIOD_SECONDS = 30
#: How many adjacent 30-second steps either side of "now" are still
#: accepted, to absorb clock drift between the server and the investigator's
#: phone -- +/-1 step (30s) is the conventional, narrow tolerance.
_VALIDATION_WINDOW_STEPS = 1


def generate_totp_secret() -> str:
    """A fresh, random base32 TOTP shared secret (unpadded, upper-case)."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**_DIGITS)).zfill(_DIGITS)


def verify_totp(secret: str, code: str, *, at_time: float | None = None) -> bool:
    """True if `code` is valid for `secret` at `at_time` (default: now), within the drift window.

    `code` must be exactly `_DIGITS` decimal digits -- a malformed
    caller-supplied value (wrong length, non-digit characters) is rejected
    outright rather than compared, so an oddly-shaped input can never
    accidentally match a computed HOTP value.
    """
    if len(code) != _DIGITS or not code.isdigit():
        return False
    now = at_time if at_time is not None else time.time()
    counter = int(now // _PERIOD_SECONDS)
    return any(
        hmac.compare_digest(_hotp(secret, counter + offset), code)
        for offset in range(-_VALIDATION_WINDOW_STEPS, _VALIDATION_WINDOW_STEPS + 1)
    )


def totp_provisioning_uri(*, secret: str, account_name: str, issuer: str = "TraceX") -> str:
    """A standard `otpauth://totp/...` URI, ready to render as a QR code.

    Follows the de facto Key URI Format every mainstream authenticator app
    (Google Authenticator, Authy, 1Password, ...) already parses -- issuer
    in both the label and the query parameter, per the format's own
    recommendation for backward compatibility with older parsers.
    """
    label = urllib.parse.quote(f"{issuer}:{account_name}")
    query = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": _DIGITS,
            "period": _PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


__all__ = ["generate_totp_secret", "verify_totp", "totp_provisioning_uri"]
