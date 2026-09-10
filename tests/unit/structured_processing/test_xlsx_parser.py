"""Scenario 7, 9 (XLSX half), 10 (XLSX half): sheet/row/column provenance, formulas
never evaluated, malformed input, limits."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured import xlsx_parser
from tests.fixtures.structured_processing.builders import build_xlsx


def test_xlsx_preserves_sheet_row_column_provenance() -> None:
    data = build_xlsx(
        headers=["caller_number", "timestamp"],
        rows=[["9876543210", "2026-01-01 10:00:00"]],
        sheet_name="CDR",
    )
    records = xlsx_parser.parse_xlsx(data)

    assert len(records) == 1
    assert records[0].values["caller_number"] == "9876543210"

    locator = records[0].locator_for("caller_number")
    assert locator.sheet == "CDR"
    assert locator.row == 2
    assert locator.column == 1


def test_xlsx_formula_cell_is_never_evaluated() -> None:
    """A formula cell's raw formula string is read as text -- never computed."""
    data = build_xlsx(
        headers=["a", "b"],
        rows=[[1, 2]],
        formula_cell=(0, 1, "=1+1"),
    )
    records = xlsx_parser.parse_xlsx(data)
    # openpyxl with data_only=False returns the formula string itself, not "2".
    assert records[0].values["b"] == "=1+1"


def test_xlsx_skips_fully_empty_rows() -> None:
    data = build_xlsx(headers=["a"], rows=[["1"], [None], ["3"]])
    records = xlsx_parser.parse_xlsx(data)
    assert [r.values["a"] for r in records] == ["1", "3"]


def test_xlsx_invalid_bytes_fail_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        xlsx_parser.parse_xlsx(b"not a real xlsx file")
    assert exc_info.value.code == ErrorCode.INVALID_XLSX


def test_xlsx_unknown_sheet_fails_safely() -> None:
    data = build_xlsx(headers=["a"], rows=[["1"]], sheet_name="Sheet1")
    with pytest.raises(ProcessingError) as exc_info:
        xlsx_parser.parse_xlsx(data, sheet_name="DoesNotExist")
    assert exc_info.value.code == ErrorCode.INVALID_XLSX


def test_xlsx_row_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx_parser, "MAX_ROWS", 1)
    data = build_xlsx(headers=["a"], rows=[["1"], ["2"]])
    with pytest.raises(ProcessingError) as exc_info:
        xlsx_parser.parse_xlsx(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED
