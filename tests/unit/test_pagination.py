"""Gap-Closure re-close (G17): `app.core.pagination` cursor primitive coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.pagination import (
    CursorError,
    CursorPosition,
    decode_cursor,
    derive_cursor_signing_key,
    encode_cursor,
)

_KEY = derive_cursor_signing_key("dev-only-change-me-32-characters-minimum-000")
_OTHER_KEY = derive_cursor_signing_key("a-completely-different-secret-value-here-ok")
_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _position(*, case_id=None) -> CursorPosition:
    return CursorPosition(case_id=case_id or uuid4(), created_at=_NOW, row_id=uuid4())


def test_cursor_round_trips() -> None:
    position = _position()
    cursor = encode_cursor(_KEY, position)
    decoded = decode_cursor(_KEY, cursor, expected_case_id=position.case_id)
    assert decoded == position


def test_cursor_from_case_a_is_rejected_for_case_b() -> None:
    """The exact property the gap register requires: a cursor issued for
    one case must never resolve for a different case, even though its
    signature is perfectly valid."""
    case_a_position = _position()
    cursor = encode_cursor(_KEY, case_a_position)
    case_b_id = uuid4()
    with pytest.raises(CursorError, match="not valid for this case"):
        decode_cursor(_KEY, cursor, expected_case_id=case_b_id)


def test_tampered_cursor_payload_is_rejected() -> None:
    position = _position()
    cursor = encode_cursor(_KEY, position)
    payload_b64, signature_b64 = cursor.split(".")
    # Flip the payload without re-signing -- simulates a caller trying to
    # edit the row_id/created_at to skip ahead or reach past a bound.
    tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    with pytest.raises(CursorError):
        decode_cursor(_KEY, f"{tampered}.{signature_b64}", expected_case_id=position.case_id)


def test_cursor_signed_with_a_different_key_is_rejected() -> None:
    position = _position()
    cursor = encode_cursor(_KEY, position)
    with pytest.raises(CursorError, match="signature is invalid"):
        decode_cursor(_OTHER_KEY, cursor, expected_case_id=position.case_id)


def test_malformed_cursor_strings_are_rejected_not_raise_uncaught() -> None:
    case_id = uuid4()
    for garbage in ("", "not-a-cursor", "a.b.c", "===.===", "a" * 40):
        with pytest.raises(CursorError):
            decode_cursor(_KEY, garbage, expected_case_id=case_id)


def test_cursor_is_opaque_and_never_contains_readable_uuids() -> None:
    """ "Opaque" means a caller can't casually read the row_id/case_id back
    out of the string without knowing this is base64 -- not literal
    plaintext UUIDs sitting in the response."""
    position = _position()
    cursor = encode_cursor(_KEY, position)
    assert str(position.row_id) not in cursor
    assert str(position.case_id) not in cursor


def test_different_positions_produce_different_cursors() -> None:
    position_a = _position()
    position_b = CursorPosition(
        case_id=position_a.case_id,
        created_at=position_a.created_at + timedelta(seconds=1),
        row_id=position_a.row_id,
    )
    assert encode_cursor(_KEY, position_a) != encode_cursor(_KEY, position_b)


def test_derive_cursor_signing_key_is_deterministic_and_not_the_raw_secret() -> None:
    secret = "dev-only-change-me-32-characters-minimum-000"
    assert derive_cursor_signing_key(secret) == derive_cursor_signing_key(secret)
    assert derive_cursor_signing_key(secret) != secret.encode("utf-8")
