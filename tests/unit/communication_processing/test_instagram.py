"""Scenario 13: Instagram parser preserves message provenance."""

from __future__ import annotations

import pytest

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.social.instagram import parse_instagram_export
from tests.fixtures.communication_processing.builders import build_instagram_export


def test_preserves_sender_participants_and_json_path() -> None:
    data = build_instagram_export(
        thread_path="inbox/alice_bob_123",
        participants=["alice", "bob"],
        messages=[{"sender_name": "alice", "timestamp_ms": 1767261600000, "content": "hello"}],
    )
    records = parse_instagram_export(data)
    assert len(records) == 1
    record = records[0]
    assert record.conversation_id == "inbox/alice_bob_123"
    assert record.sender == "alice"
    assert record.participants == ("alice", "bob")
    assert record.text == "hello"
    assert record.locator.json_path == "$.messages[0]"


def test_timestamp_ms_is_unambiguous_utc() -> None:
    data = build_instagram_export(
        messages=[{"sender_name": "alice", "timestamp_ms": 1767261600000, "content": "hi"}]
    )
    record = parse_instagram_export(data)[0]
    assert record.timestamp_utc is not None
    assert record.timestamp_utc.year == 2026


def test_no_message_id_still_has_non_empty_provenance() -> None:
    data = build_instagram_export(
        messages=[{"sender_name": "alice", "timestamp_ms": 1767261600000, "content": "hi"}]
    )
    record = parse_instagram_export(data)[0]
    assert record.message_id is None
    assert record.locator.json_path is not None  # non-empty provenance via json_path alone


def test_malformed_json_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_instagram_export(b"not json")
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT


def test_no_messages_fails_safely() -> None:
    data = build_instagram_export(messages=[])
    with pytest.raises(ProcessingError) as exc_info:
        parse_instagram_export(data)
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT
