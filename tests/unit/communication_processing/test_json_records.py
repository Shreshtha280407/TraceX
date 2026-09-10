"""Scenario 14: generic social-intelligence JSON record parsing works."""

from __future__ import annotations

import pytest

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.social.json_records import parse_generic_json_export
from tests.fixtures.communication_processing.builders import build_generic_json_export


def test_parses_full_record_shape() -> None:
    data = build_generic_json_export(
        records=[
            {
                "message_id": "m1",
                "conversation_id": "c1",
                "sender": "alice",
                "participants": ["alice", "bob"],
                "timestamp": "2026-01-01T10:00:00Z",
                "text": "hi",
                "reply_to": None,
            }
        ]
    )
    records = parse_generic_json_export(data)
    assert len(records) == 1
    record = records[0]
    assert record.message_id == "m1"
    assert record.conversation_id == "c1"
    assert record.participants == ("alice", "bob")
    assert record.timestamp_utc is not None
    assert record.locator.json_path == "$.records[0]"


def test_explicit_offset_timestamp_normalizes_to_utc() -> None:
    data = build_generic_json_export(
        records=[{"message_id": "m1", "timestamp": "2026-01-01T15:30:00+05:30", "text": "hi"}]
    )
    record = parse_generic_json_export(data)[0]
    assert record.timestamp_utc is not None
    assert record.timestamp_utc.hour == 10  # 15:30 +05:30 == 10:00 UTC


def test_ambiguous_timestamp_is_preserved_raw_only() -> None:
    """Scenario 15 (timezone half): a naive timestamp with no offset is never guessed."""
    data = build_generic_json_export(
        records=[{"message_id": "m1", "timestamp": "2026-01-01 10:00:00", "text": "hi"}]
    )
    record = parse_generic_json_export(data)[0]
    assert record.timestamp_utc is None
    assert record.timestamp_raw == "2026-01-01 10:00:00"


def test_malformed_json_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_generic_json_export(b"{bad json")
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT


def test_missing_records_key_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_generic_json_export(b'{"platform": "x"}')
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT
