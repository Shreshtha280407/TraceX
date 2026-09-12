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
    assert record.timestamp_source_timezone is None  # explicit "Z": nothing to default
    assert record.locator.json_path == "$.records[0]"


def test_explicit_offset_timestamp_normalizes_to_utc() -> None:
    data = build_generic_json_export(
        records=[{"message_id": "m1", "timestamp": "2026-01-01T15:30:00+05:30", "text": "hi"}]
    )
    record = parse_generic_json_export(data)[0]
    assert record.timestamp_utc is not None
    assert record.timestamp_utc.hour == 10  # 15:30 +05:30 == 10:00 UTC
    assert record.timestamp_source_timezone is None  # explicit offset: nothing to default


def test_naive_timestamp_resolves_via_default_timezone() -> None:
    """Scenario 18: a naive timestamp with no offset uses the documented default zone."""
    data = build_generic_json_export(
        records=[{"message_id": "m1", "timestamp": "2026-01-01 10:00:00", "text": "hi"}]
    )
    record = parse_generic_json_export(data)[0]
    assert record.timestamp_raw == "2026-01-01 10:00:00"
    assert record.timestamp_source_timezone == "Asia/Kolkata"
    assert record.timestamp_utc is not None
    assert record.timestamp_utc.hour == 4  # 10:00 IST (+05:30) == 04:30 UTC
    assert record.timestamp_utc.minute == 30


def test_unparseable_timestamp_is_preserved_raw_only() -> None:
    """A string that isn't valid ISO-8601 at all is never guessed at."""
    data = build_generic_json_export(
        records=[{"message_id": "m1", "timestamp": "not-a-timestamp", "text": "hi"}]
    )
    record = parse_generic_json_export(data)[0]
    assert record.timestamp_utc is None
    assert record.timestamp_source_timezone is None
    assert record.timestamp_raw == "not-a-timestamp"


def test_malformed_json_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_generic_json_export(b"{bad json")
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT


def test_missing_records_key_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_generic_json_export(b'{"platform": "x"}')
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT


def test_non_dict_entry_is_skipped_without_corrupting_valid_neighbours() -> None:
    """Scenario 16: a malformed entry is skipped, never fatal to the whole batch."""
    data = build_generic_json_export(
        records=[
            {"message_id": "m1", "text": "first"},
            "not a record object",  # type: ignore[list-item]
            42,  # type: ignore[list-item]
            {"message_id": "m2", "text": "second"},
        ]
    )
    records = parse_generic_json_export(data)
    assert [r.message_id for r in records] == ["m1", "m2"]


def test_record_with_only_optional_fields_missing_parses_safely() -> None:
    """Scenario 12: a record missing every optional field is safely accepted with
    explicit None values, never crashes and never fabricates a value."""
    data = build_generic_json_export(records=[{"message_id": "m1"}])
    record = parse_generic_json_export(data)[0]
    assert record.message_id == "m1"
    assert record.sender is None
    assert record.text is None
    assert record.timestamp_utc is None
    assert record.participants == ()
