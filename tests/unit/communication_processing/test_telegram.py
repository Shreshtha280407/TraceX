"""Scenario 12: Telegram parser preserves message_id and json_path."""

from __future__ import annotations

import pytest

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.social.telegram import parse_telegram_export
from tests.fixtures.communication_processing.builders import build_telegram_export


def test_preserves_message_id_and_json_path() -> None:
    data = build_telegram_export(
        chat_id=42,
        messages=[
            {
                "id": 1001,
                "type": "message",
                "date": "2026-01-01T10:00:00",
                "date_unixtime": "1767261600",
                "from": "Alice",
                "text": "hi",
            }
        ],
    )
    records = parse_telegram_export(data)
    assert len(records) == 1
    record = records[0]
    assert record.message_id == "1001"
    assert record.conversation_id == "42"
    assert record.locator.json_path == "$.messages[0]"


def test_date_unixtime_is_unambiguous_utc() -> None:
    data = build_telegram_export(
        messages=[
            {
                "id": 1,
                "type": "message",
                "date": "2026-01-01T10:00:00",
                "date_unixtime": "1767261600",
                "text": "hi",
            }
        ]
    )
    record = parse_telegram_export(data)[0]
    assert record.timestamp_utc is not None
    assert record.timestamp_raw == "2026-01-01T10:00:00"
    assert record.timestamp_source_timezone is None  # date_unixtime: nothing to default


def test_naive_date_without_unixtime_resolves_via_default_timezone() -> None:
    """Scenario 18: no `date_unixtime` at all falls back to the documented default zone."""
    data = build_telegram_export(
        messages=[{"id": 1, "type": "message", "date": "2026-01-01T10:00:00", "text": "hi"}]
    )
    record = parse_telegram_export(data)[0]
    assert record.timestamp_utc is not None
    assert record.timestamp_source_timezone == "Asia/Kolkata"
    assert record.timestamp_utc.hour == 4  # 10:00 IST (+05:30) == 04:30 UTC
    assert record.timestamp_utc.minute == 30


def test_reply_to_message_id_preserved() -> None:
    data = build_telegram_export(
        messages=[
            {"id": 1, "type": "message", "date": "2026-01-01T10:00:00", "text": "first"},
            {
                "id": 2,
                "type": "message",
                "date": "2026-01-01T10:01:00",
                "text": "reply",
                "reply_to_message_id": 1,
            },
        ]
    )
    records = parse_telegram_export(data)
    assert records[1].reply_to == "1"


def test_service_messages_are_skipped() -> None:
    data = build_telegram_export(
        messages=[
            {"id": 1, "type": "service", "date": "2026-01-01T10:00:00"},
            {"id": 2, "type": "message", "date": "2026-01-01T10:01:00", "text": "real message"},
        ]
    )
    records = parse_telegram_export(data)
    assert len(records) == 1
    assert records[0].message_id == "2"


def test_malformed_json_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_telegram_export(b"{not valid json")
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT


def test_missing_messages_array_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_telegram_export(b'{"name": "x"}')
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT
