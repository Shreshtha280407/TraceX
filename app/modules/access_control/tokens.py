"""Access-token (signed JWT) and refresh-token (opaque secret) primitives.

Access tokens are short-lived, statelessly-verifiable JWTs (see
`AccessTokenClaims`): no case membership, clearance, or other
frequently-changing authorization data is embedded in them -- only identity
(`sub`) and a session reference (`sid`), per
`docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`.
Refresh tokens are high-entropy opaque secrets; only their SHA-256 hash is
ever persisted or compared (`sessions.py`/`repository.py`) -- the raw
secret exists only in the response body handed to the caller once, and is
never logged.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID

import jwt
from pydantic import ValidationError as PydanticValidationError

from app.modules.access_control.errors import InvalidTokenError
from app.modules.access_control.models import AccessTokenClaims, MfaChallengeClaims, TokenType

#: Bytes of entropy for a generated refresh-token secret (256 bits).
REFRESH_TOKEN_BYTES = 32

_REQUIRED_CLAIMS = ("sub", "sid", "iat", "exp", "iss", "aud", "typ")
_REQUIRED_MFA_CHALLENGE_CLAIMS = ("sub", "iat", "exp", "iss", "aud", "typ")


def create_access_token(
    *,
    user_id: UUID,
    session_id: UUID,
    secret: str,
    algorithm: str,
    issuer: str,
    audience: str,
    ttl_seconds: int,
    now: datetime,
) -> str:
    """Mint a short-lived signed access token.

    `now` is caller-supplied (never read from the wall clock internally) so
    callers -- and tests -- have full, deterministic control over `iat`/`exp`.
    """
    claims = {
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
        "iss": issuer,
        "aud": audience,
        "typ": TokenType.ACCESS_V1.value,
    }
    return jwt.encode(claims, secret, algorithm=algorithm)


def decode_access_token(
    token: str,
    *,
    secret: str,
    algorithm: str,
    issuer: str,
    audience: str,
) -> AccessTokenClaims:
    """Decode and fully validate an access token.

    Checks signature, issuer, audience, expiry, presence of every claim
    this module relies on, and the `typ` claim -- raising `InvalidTokenError`
    for every failure mode. Never surfaces the underlying PyJWT exception
    text or the raw token to a caller (both could appear in logs/responses
    otherwise).
    """
    try:
        raw_claims = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            issuer=issuer,
            audience=audience,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("access token failed validation") from exc

    try:
        claims = AccessTokenClaims.model_validate(raw_claims)
    except PydanticValidationError as exc:
        raise InvalidTokenError("access token claims are malformed") from exc

    if claims.typ is not TokenType.ACCESS_V1:
        raise InvalidTokenError("unexpected access token type")
    return claims


def create_mfa_challenge_token(
    *,
    user_id: UUID,
    secret: str,
    algorithm: str,
    issuer: str,
    audience: str,
    ttl_seconds: int,
    now: datetime,
) -> str:
    """Mint a short-lived "password already verified, MFA still owed" token.

    Deliberately carries no session ID: `login` has not created a session
    yet at the point this is issued, and never will unless the caller comes
    back with a valid code (see `service.verify_mfa_login`).
    """
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
        "iss": issuer,
        "aud": audience,
        "typ": TokenType.MFA_PENDING.value,
    }
    return jwt.encode(claims, secret, algorithm=algorithm)


def decode_mfa_challenge_token(
    token: str,
    *,
    secret: str,
    algorithm: str,
    issuer: str,
    audience: str,
) -> MfaChallengeClaims:
    """Decode and fully validate an MFA-challenge token. Mirrors `decode_access_token`."""
    try:
        raw_claims = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            issuer=issuer,
            audience=audience,
            options={"require": list(_REQUIRED_MFA_CHALLENGE_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("mfa challenge token failed validation") from exc

    try:
        claims = MfaChallengeClaims.model_validate(raw_claims)
    except PydanticValidationError as exc:
        raise InvalidTokenError("mfa challenge token claims are malformed") from exc

    if claims.typ is not TokenType.MFA_PENDING:
        raise InvalidTokenError("unexpected mfa challenge token type")
    return claims


def generate_refresh_token() -> str:
    """A fresh, high-entropy opaque refresh-token secret.

    Never stored raw -- see `hash_refresh_token`. Callers must treat the
    return value the same as a password: return it to the caller once,
    never log it.
    """
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(refresh_token: str) -> str:
    """SHA-256 hex digest of a refresh token, for storage/comparison.

    A plain, unsalted, unkeyed cryptographic hash is appropriate here
    specifically *because* the input is a 256-bit-entropy random secret,
    not a low-entropy user-chosen password: there is no dictionary/rainbow
    -table attack surface to defend against with a slow KDF (unlike
    `password.py`, which must use Argon2id). Hashing here only needs to be
    a fast, deterministic way to compare "does this request's token match a
    stored session" without keeping the raw secret in the database.
    """
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
