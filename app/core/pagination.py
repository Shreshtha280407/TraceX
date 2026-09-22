"""Gap-Closure re-close (G17): opaque, case-bound, tamper-evident cursor pagination.

Replaces this codebase's earlier `limit`+`offset`-only pagination on
select collection endpoints with real keyset pagination: a cursor names
the last row a caller has already seen (`created_at`, `row_id`), not a
numeric skip count, so pagination stays correct even if rows are added
between pages, and is HMAC-signed so a caller can never forge or tamper
with it to read past a bound or reach into another case.

**Case-bound**: the cursor embeds the `case_id` it was issued for.
`decode_cursor` requires the caller's own `case_id` and rejects (raises
`CursorError`) any cursor issued for a different case, even though the
signature itself would still verify -- this is the property that makes
"a cursor from Case A is rejected for Case B" true, not merely "the
cursor is well-formed."

**Tamper-evident**: the payload (`case_id`, `created_at`, `row_id`) is
HMAC-SHA256-signed with a key derived from `Settings.auth_jwt_secret`
(domain-separated via a fixed prefix, never the raw JWT secret reused
directly for a different purpose) using constant-time comparison
(`hmac.compare_digest`) on decode.

**No new infrastructure**: pure stdlib (`hmac`, `hashlib`, `base64`,
`json`) -- no cursor state is stored anywhere; the cursor *is* the state,
entirely reconstructed from what the caller presents back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_KEY_DOMAIN_PREFIX = b"tracex-cursor-v1:"


class CursorError(ValueError):
    """A cursor is malformed, its signature doesn't verify, or it names a
    different case than the caller is querying. Always a client error
    (422), never a server fault -- see each route's own error handling."""


@dataclass(frozen=True)
class CursorPosition:
    """The last row a caller has already seen: keyset pagination resumes
    strictly after this `(created_at, row_id)` position, never by a
    numeric offset that a concurrent insert/delete could shift."""

    case_id: UUID
    created_at: datetime
    row_id: UUID


def derive_cursor_signing_key(auth_jwt_secret: str) -> bytes:
    """A domain-separated key derived from the JWT secret -- never the raw
    secret reused directly for a second, different purpose."""
    return hashlib.sha256(_KEY_DOMAIN_PREFIX + auth_jwt_secret.encode("utf-8")).digest()


def _sign(key: bytes, payload: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


def encode_cursor(key: bytes, position: CursorPosition) -> str:
    """Build an opaque cursor string for `position`. Never includes
    anything beyond `case_id`/`created_at`/`row_id` -- no note text, no
    rationale, no evidence content; only what's needed to resume a page."""
    payload = json.dumps(
        {
            "case_id": str(position.case_id),
            "created_at": position.created_at.isoformat(),
            "row_id": str(position.row_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = _sign(key, payload)
    return (
        base64.urlsafe_b64encode(payload).decode("ascii")
        + "."
        + base64.urlsafe_b64encode(signature).decode("ascii")
    )


def decode_cursor(key: bytes, cursor: str, *, expected_case_id: UUID) -> CursorPosition:
    """Decode and verify `cursor`. Raises `CursorError` for anything short
    of a fully valid, signature-matching, correctly-cased cursor --
    never returns a partially-trusted position."""
    parts = cursor.split(".")
    if len(parts) != 2:
        raise CursorError("malformed cursor")
    payload_b64, signature_b64 = parts
    try:
        payload = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        signature = base64.urlsafe_b64decode(signature_b64.encode("ascii"))
    except Exception as exc:
        raise CursorError("malformed cursor") from exc

    expected_signature = _sign(key, payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise CursorError("cursor signature is invalid")

    try:
        data = json.loads(payload)
        position = CursorPosition(
            case_id=UUID(data["case_id"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            row_id=UUID(data["row_id"]),
        )
    except Exception as exc:
        raise CursorError("malformed cursor payload") from exc

    if position.case_id != expected_case_id:
        raise CursorError("cursor is not valid for this case")
    return position


def normalize_datetime(value: datetime) -> datetime:
    """A cursor's `created_at` always round-trips through ISO-8601 text; a
    naive datetime (no tzinfo) is assumed UTC -- this codebase's own
    convention for every timezone-aware column."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "CursorError",
    "CursorPosition",
    "decode_cursor",
    "derive_cursor_signing_key",
    "encode_cursor",
    "normalize_datetime",
]
