"""Scenario 15 (limits/unsupported half) and 16.

Chat parsing safety limits and no leaked content.
"""

from __future__ import annotations

import json

import pytest

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.social import json_records as json_records_module
from app.modules.communication_processing.social import whatsapp as whatsapp_module
from app.modules.communication_processing.social.json_records import parse_generic_json_export
from app.modules.communication_processing.social.whatsapp import parse_whatsapp_export
from tests.fixtures.communication_processing.builders import build_generic_json_export


def test_oversized_export_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module, "MAX_CHAT_EXPORT_BYTES", 10)
    with pytest.raises(ProcessingError) as exc_info:
        parse_whatsapp_export(b"01/01/26, 10:00 - Alice: this export is too big")
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_too_many_lines_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module, "MAX_CHAT_LINES", 2)
    data = "\n".join(f"line {i}" for i in range(10)).encode()
    with pytest.raises(ProcessingError) as exc_info:
        parse_whatsapp_export(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_too_many_messages_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module, "MAX_CHAT_MESSAGES", 1)
    data = (
        b"01/01/26, 10:00 - Alice: one\n01/01/26, 10:01 - Bob: two\n01/01/26, 10:02 - Alice: three"
    )
    with pytest.raises(ProcessingError) as exc_info:
        parse_whatsapp_export(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_over_nested_json_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_records_module, "MAX_CHAT_JSON_DEPTH", 3)
    nested: dict[str, object] = {"x": 1}
    for _ in range(10):
        nested = {"x": nested}
    data = json.dumps({"platform": "generic", "records": [], "nested": nested}).encode()
    with pytest.raises(ProcessingError) as exc_info:
        parse_generic_json_export(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_too_many_records_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_records_module, "MAX_CHAT_MESSAGES", 1)
    data = build_generic_json_export(
        records=[{"message_id": "1", "text": "a"}, {"message_id": "2", "text": "b"}]
    )
    with pytest.raises(ProcessingError) as exc_info:
        parse_generic_json_export(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_error_messages_never_contain_sensitive_content() -> None:
    """A sensitive value present in a malformed export must never appear in the error."""
    secret_phone = "9998887776"
    data = f"garbage line mentioning {secret_phone} with no timestamp prefix".encode()
    try:
        parse_whatsapp_export(data)
        raise AssertionError("expected ProcessingError")
    except ProcessingError as exc:
        assert secret_phone not in exc.message
        assert secret_phone not in exc.code


def test_error_never_contains_full_export_content() -> None:
    huge_secret_text = "SENSITIVE_CONTENT_MARKER_" + ("x" * 500)
    data = f"garbage {huge_secret_text}".encode()
    try:
        parse_whatsapp_export(data)
        raise AssertionError("expected ProcessingError")
    except ProcessingError as exc:
        assert huge_secret_text not in exc.message
        assert len(exc.message) < 200
