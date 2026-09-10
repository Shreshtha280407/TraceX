"""Scenario 11: WhatsApp parser accepts the documented fixture and preserves source location."""

from __future__ import annotations

import pytest

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.social.whatsapp import parse_whatsapp_export
from tests.fixtures.communication_processing.builders import build_whatsapp_export


def test_parses_sender_and_text() -> None:
    data = build_whatsapp_export(["01/01/26, 10:00 - Alice: Hello there"])
    records = parse_whatsapp_export(data)
    assert len(records) == 1
    record = records[0]
    assert record.platform == "whatsapp"
    assert record.sender == "Alice"
    assert record.text == "Hello there"
    assert record.timestamp_raw == "01/01/26, 10:00"
    assert record.timestamp_utc is None  # no timezone in WhatsApp exports: never guessed


def test_system_message_with_no_sender() -> None:
    data = build_whatsapp_export(["01/01/26, 10:00 - Messages and calls are end-to-end encrypted."])
    records = parse_whatsapp_export(data)
    assert records[0].sender is None
    assert records[0].text == "Messages and calls are end-to-end encrypted."


def test_continuation_lines_are_appended_to_previous_message() -> None:
    data = build_whatsapp_export(
        ["01/01/26, 10:00 - Alice: Line one", "Line two", "01/01/26, 10:01 - Bob: Reply"]
    )
    records = parse_whatsapp_export(data)
    assert len(records) == 2
    assert records[0].text == "Line one\nLine two"
    assert records[1].text == "Reply"


def test_source_locator_spans_are_exact_and_non_overlapping() -> None:
    lines = ["01/01/26, 10:00 - Alice: First", "01/01/26, 10:01 - Bob: Second"]
    data = build_whatsapp_export(lines)
    text = data.decode("utf-8")
    records = parse_whatsapp_export(data)

    for record in records:
        span_start = record.locator.span_start
        span_end = record.locator.span_end
        assert span_start is not None and span_end is not None
        assert span_end > span_start
        assert text[span_start:span_end]  # non-empty exact slice


def test_no_recognizable_lines_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        parse_whatsapp_export(b"this is not a whatsapp export at all")
    assert exc_info.value.code == ErrorCode.MALFORMED_CHAT_EXPORT
