"""Scenario 6, 9 (CSV half), 10 (CSV half): row/column provenance, malformed input, limits."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured import csv_parser


def test_csv_preserves_row_and_column_provenance() -> None:
    data = (
        b"caller_number,timestamp\n9876543210,2026-01-01 10:00:00\n9123456789,2026-01-02 11:00:00\n"
    )
    records = csv_parser.parse_csv(data)

    assert len(records) == 2
    assert records[0].index == 0
    assert records[0].values == {"caller_number": "9876543210", "timestamp": "2026-01-01 10:00:00"}

    locator = records[0].locator_for("caller_number")
    assert locator.row == 2  # header is row 1
    assert locator.column == 1

    second_locator = records[1].locator_for("timestamp")
    assert second_locator.row == 3
    assert second_locator.column == 2


def test_csv_whole_record_locator_has_no_column() -> None:
    data = b"a,b\n1,2\n"
    records = csv_parser.parse_csv(data)
    locator = records[0].locator_for(None)
    assert locator.row == 2
    assert locator.column is None


def test_csv_missing_header_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        csv_parser.parse_csv(b"")
    assert exc_info.value.code == ErrorCode.MALFORMED_CSV


def test_csv_row_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csv_parser, "MAX_ROWS", 2)
    data = b"a\n1\n2\n3\n"
    with pytest.raises(ProcessingError) as exc_info:
        csv_parser.parse_csv(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_csv_column_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csv_parser, "MAX_COLUMNS", 2)
    with pytest.raises(ProcessingError) as exc_info:
        csv_parser.parse_csv(b"a,b,c\n1,2,3\n")
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_csv_cell_length_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csv_parser, "MAX_CELL_TEXT_LENGTH", 5)
    with pytest.raises(ProcessingError) as exc_info:
        csv_parser.parse_csv(b"a\ntoolongvalue\n")
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_csv_duplicate_headers_first_occurrence_wins() -> None:
    records = csv_parser.parse_csv(b"a,a\n1,2\n")
    assert records[0].values == {"a": "1"}
