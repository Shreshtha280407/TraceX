"""Scenario 11: CDR profile handles valid aliases and rejects missing required fields."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured.cdr import normalize_cdr_records
from app.modules.structured_processing.structured.csv_parser import parse_csv


def test_cdr_resolves_header_aliases_case_and_spacing_insensitively() -> None:
    data = b"Caller Number,Callee Number,Timestamp\n9876543210,9123456789,2026-01-01 10:00:00\n"
    mentions = normalize_cdr_records(parse_csv(data))
    record_mentions = [m for m in mentions if m.observation_type == "cdr_call_record"]
    assert len(record_mentions) == 1
    # Phase 3: normalized to E.164 (India is this codebase's only known
    # country context) -- the original value is always kept alongside.
    assert record_mentions[0].attributes["caller_number"] == "+919876543210"
    assert record_mentions[0].attributes["caller_number_raw"] == "9876543210"
    assert record_mentions[0].attributes["callee_number"] == "+919123456789"
    assert record_mentions[0].attributes["callee_number_raw"] == "9123456789"


def test_cdr_normalizes_unambiguous_indian_mobile_number_to_e164() -> None:
    data = b"caller_number,timestamp\n+91-9876543210,2026-01-01 10:00:00\n"
    mentions = normalize_cdr_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "cdr_call_record")
    assert record.attributes["caller_number"] == "+919876543210"
    assert record.attributes["caller_number_raw"] == "+91-9876543210"


def test_cdr_retains_original_value_when_normalization_is_uncertain() -> None:
    data = b"caller_number,timestamp\nUNKNOWN-CALLER,2026-01-01 10:00:00\n"
    mentions = normalize_cdr_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "cdr_call_record")
    assert record.attributes["caller_number"] == "UNKNOWN-CALLER"
    assert record.attributes["caller_number_raw"] == "UNKNOWN-CALLER"


def test_cdr_timestamp_uses_the_configured_default_timezone_when_none_is_given() -> None:
    data = b"caller_number,timestamp\n9876543210,2026-01-01 10:00:00\n"
    mentions = normalize_cdr_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "cdr_call_record")
    # Asia/Kolkata (UTC+05:30) is the configured default; 10:00 IST -> 04:30 UTC.
    assert record.attributes["timestamp"] == "2026-01-01T04:30:00+00:00"
    assert record.attributes["timestamp_source_timezone"] == "Asia/Kolkata"
    assert record.attributes["timestamp_source_utc_offset"] == "+0530"


def test_cdr_timestamp_respects_an_explicit_source_timezone() -> None:
    data = b"caller_number,timestamp,source_timezone\n9876543210,2026-01-01 10:00:00,UTC\n"
    mentions = normalize_cdr_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "cdr_call_record")
    assert record.attributes["timestamp"] == "2026-01-01T10:00:00+00:00"
    assert record.attributes["timestamp_source_timezone"] == "UTC"


def test_cdr_timestamp_respects_an_explicit_fixed_offset() -> None:
    data = b"caller_number,timestamp,source_timezone\n9876543210,2026-01-01 10:00:00,+02:00\n"
    mentions = normalize_cdr_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "cdr_call_record")
    assert record.attributes["timestamp"] == "2026-01-01T08:00:00+00:00"
    assert record.attributes["timestamp_source_utc_offset"] == "+0200"


def test_cdr_rejects_record_missing_required_fields() -> None:
    data = b"callee_number,duration_seconds\n9123456789,60\n"  # no caller_number, no timestamp
    with pytest.raises(ProcessingError) as exc_info:
        normalize_cdr_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.REQUIRED_FIELD_MISSING


def test_cdr_rejects_unparseable_timestamp() -> None:
    data = b"caller_number,timestamp\n9876543210,not-a-date\n"
    with pytest.raises(ProcessingError) as exc_info:
        normalize_cdr_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.REQUIRED_FIELD_MISSING


def test_cdr_preserves_duration_only_when_numeric() -> None:
    valid = b"caller_number,timestamp,duration_seconds\n9876543210,2026-01-01 10:00:00,120\n"
    record = next(
        m
        for m in normalize_cdr_records(parse_csv(valid))
        if m.observation_type == "cdr_call_record"
    )
    assert record.attributes["duration_seconds"] == 120.0

    invalid = b"caller_number,timestamp,duration_seconds\n9876543210,2026-01-01 10:00:00,n/a\n"
    record2 = next(
        m
        for m in normalize_cdr_records(parse_csv(invalid))
        if m.observation_type == "cdr_call_record"
    )
    assert "duration_seconds" not in record2.attributes
    assert record2.attributes["duration_seconds_raw"] == "n/a"


def test_cdr_emits_device_subscriber_and_tower_mentions() -> None:
    data = (
        b"caller_number,timestamp,imei,imsi,cell_tower_id\n"
        b"9876543210,2026-01-01 10:00:00,356789012345678,404123456789012,TWR-9\n"
    )
    mentions = normalize_cdr_records(parse_csv(data))
    types = {m.observation_type for m in mentions}
    assert types == {
        "cdr_call_record",
        "cdr_device_identifier_mention",
        "cdr_subscriber_identifier_mention",
        "cdr_tower_mention",
    }


def test_cdr_no_event_or_entity_type_is_ever_created() -> None:
    """CDR normalization only ever emits mentions -- never EntityV1/EventV1 shapes."""
    data = b"caller_number,timestamp\n9876543210,2026-01-01 10:00:00\n"
    mentions = normalize_cdr_records(parse_csv(data))
    for mention in mentions:
        assert mention.observation_type.startswith("cdr_")
