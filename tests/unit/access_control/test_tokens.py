"""Scenario 6: access tokens reject bad signature, issuer, audience, expiry, type, or claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.modules.access_control.errors import InvalidTokenError
from app.modules.access_control.tokens import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)

_SECRET = "x" * 32
_ALGORITHM = "HS256"
_ISSUER = "tracex-api-test"
_AUDIENCE = "tracex-clients-test"


def _make_token(**overrides: object) -> str:
    now = datetime.now(UTC)
    kwargs: dict[str, object] = {
        "user_id": uuid4(),
        "session_id": uuid4(),
        "secret": _SECRET,
        "algorithm": _ALGORITHM,
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "ttl_seconds": 900,
        "now": now,
    }
    kwargs.update(overrides)
    return create_access_token(**kwargs)  # type: ignore[arg-type]


def _decode(token: str, **overrides: object) -> object:
    kwargs: dict[str, object] = {
        "secret": _SECRET,
        "algorithm": _ALGORITHM,
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
    }
    kwargs.update(overrides)
    return decode_access_token(token, **kwargs)  # type: ignore[arg-type]


def test_valid_token_round_trips() -> None:
    user_id, session_id = uuid4(), uuid4()
    token = _make_token(user_id=user_id, session_id=session_id)
    claims = _decode(token)
    assert claims.sub == user_id  # type: ignore[union-attr]
    assert claims.sid == session_id  # type: ignore[union-attr]


def test_rejects_invalid_signature() -> None:
    token = _make_token()
    with pytest.raises(InvalidTokenError):
        _decode(token, secret="y" * 32)


def test_rejects_wrong_issuer() -> None:
    token = _make_token()
    with pytest.raises(InvalidTokenError):
        _decode(token, issuer="someone-else")


def test_rejects_wrong_audience() -> None:
    token = _make_token()
    with pytest.raises(InvalidTokenError):
        _decode(token, audience="someone-else")


def test_rejects_expired_token() -> None:
    expired_now = datetime.now(UTC) - timedelta(hours=1)
    token = _make_token(now=expired_now, ttl_seconds=1)
    with pytest.raises(InvalidTokenError):
        _decode(token)


def test_rejects_wrong_token_type() -> None:
    now = datetime.now(UTC)
    claims = {
        "sub": str(uuid4()),
        "sid": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "typ": "refresh_v1",  # not a real refresh-token format -- just the wrong `typ` value
    }
    token = jwt.encode(claims, _SECRET, algorithm=_ALGORITHM)
    with pytest.raises(InvalidTokenError):
        _decode(token)


@pytest.mark.parametrize("missing_claim", ["sub", "sid", "iat", "exp", "iss", "aud", "typ"])
def test_rejects_malformed_claims_missing_required_field(missing_claim: str) -> None:
    now = datetime.now(UTC)
    claims = {
        "sub": str(uuid4()),
        "sid": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "typ": "access_v1",
    }
    del claims[missing_claim]
    token = jwt.encode(claims, _SECRET, algorithm=_ALGORITHM)
    with pytest.raises(InvalidTokenError):
        _decode(token)


def test_rejects_malformed_claims_bad_sub_uuid() -> None:
    now = datetime.now(UTC)
    claims = {
        "sub": "not-a-uuid",
        "sid": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "typ": "access_v1",
    }
    token = jwt.encode(claims, _SECRET, algorithm=_ALGORITHM)
    with pytest.raises(InvalidTokenError):
        _decode(token)


def test_rejects_garbage_token() -> None:
    with pytest.raises(InvalidTokenError):
        _decode("not-a-jwt-at-all")


def test_never_accepts_the_none_algorithm() -> None:
    # `alg: none` with an empty signature is the classic JWT bypass attempt.
    header = '{"alg":"none","typ":"JWT"}'
    now = datetime.now(UTC)
    import base64
    import json

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    payload = json.dumps(
        {
            "sub": str(uuid4()),
            "sid": str(uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "typ": "access_v1",
        }
    )
    forged = f"{_b64(header.encode())}.{_b64(payload.encode())}."
    with pytest.raises(InvalidTokenError):
        _decode(forged)


def test_refresh_token_is_opaque_high_entropy_and_hashed_not_reversible() -> None:
    token_a = generate_refresh_token()
    token_b = generate_refresh_token()
    assert token_a != token_b
    assert len(token_a) >= 32

    hashed = hash_refresh_token(token_a)
    assert hashed != token_a
    assert token_a not in hashed
    # Deterministic: the same raw token always hashes the same way (needed
    # for lookup-by-hash), but a different token never collides.
    assert hash_refresh_token(token_a) == hashed
    assert hash_refresh_token(token_b) != hashed
