"""Scenarios 15-19: vectorized chunked CDR/finance reading and the malformed-row policy."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured.chunked_processing import (
    assess_schema,
    iter_csv_record_chunks,
    iter_xlsx_record_chunks,
    normalize_chunk,
    peek_csv_header,
    peek_xlsx_header,
)
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FINANCIAL_TRANSACTION_GENERIC_V1,
)


def _cdr_csv(row_count: int) -> bytes:
    data = b"caller_number,callee_number,timestamp,duration_seconds\n"
    for i in range(row_count):
        data += f"98765432{i:02d},91234567{i:02d},2026-01-01 10:{i % 60:02d}:00,{i * 5}\n".encode()
    return data


def test_schema_assessment_accepts_a_header_resolving_required_fields() -> None:
    header = peek_csv_header(_cdr_csv(1))
    assess_schema(CDR_GENERIC_V1, header)  # does not raise


def test_schema_assessment_rejects_a_header_missing_required_fields() -> None:
    header = ["some_column", "another_column"]
    with pytest.raises(ProcessingError) as exc_info:
        assess_schema(CDR_GENERIC_V1, header)
    assert exc_info.value.code == ErrorCode.AMBIGUOUS_SCHEMA
    assert "caller_number" in exc_info.value.message


def test_csv_chunks_are_bounded_by_batch_size_and_cover_every_row() -> None:
    data = _cdr_csv(23)
    chunk_sizes = [len(chunk) for chunk in iter_csv_record_chunks(data, batch_size=10)]
    assert chunk_sizes == [10, 10, 3]
    assert sum(chunk_sizes) == 23


def test_csv_chunk_row_numbers_are_globally_consistent_across_chunk_boundaries() -> None:
    data = _cdr_csv(12)
    all_records = [
        record for chunk in iter_csv_record_chunks(data, batch_size=5) for record in chunk
    ]
    row_numbers = [record.locator_for(None).row for record in all_records]
    assert row_numbers == list(range(2, 14))  # row 1 is the header


def test_malformed_row_is_reported_safely_and_does_not_abort_the_chunk() -> None:
    data = (
        b"caller_number,callee_number,timestamp\n"
        b"9876543210,9123456789,2026-01-01 10:00:00\n"
        b",9123456789,2026-01-01 10:05:00\n"  # missing caller_number
        b"9876543211,9123456788,2026-01-01 10:10:00\n"
    )
    header = peek_csv_header(data)
    assess_schema(CDR_GENERIC_V1, header)

    total_valid, total_malformed = 0, []
    for chunk in iter_csv_record_chunks(data, batch_size=10):
        result = normalize_chunk(CDR_GENERIC_V1, chunk)
        total_valid += result.valid_row_count
        total_malformed.extend(result.malformed_rows)

    assert total_valid == 2
    assert len(total_malformed) == 1
    assert total_malformed[0].error_code == ErrorCode.REQUIRED_FIELD_MISSING
    assert total_malformed[0].locator.row == 3
    # Safe: never the raw missing value, only field names.
    assert "caller_number" in total_malformed[0].reason


def test_xlsx_chunks_preserve_sheet_and_row_provenance() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "CallLog"
    sheet.append(["caller_number", "callee_number", "timestamp"])
    for i in range(5):
        sheet.append([f"98765432{i:02d}", f"91234567{i:02d}", "2026-01-01 10:00:00"])
    buf = io.BytesIO()
    workbook.save(buf)
    data = buf.getvalue()

    header = peek_xlsx_header(data)
    assess_schema(CDR_GENERIC_V1, header)

    all_records = [
        record for chunk in iter_xlsx_record_chunks(data, batch_size=2) for record in chunk
    ]
    assert len(all_records) == 5
    for record in all_records:
        locator = record.locator_for(None)
        assert locator.sheet == "CallLog"
        assert locator.row is not None


def test_xlsx_malformed_row_reported_with_sheet_provenance() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Txns"
    sheet.append(["sender_account", "receiver_account", "amount", "currency", "timestamp"])
    sheet.append(["sender-A", "receiver-B", 500, "INR", "2026-01-01 10:00:00"])
    sheet.append(["sender-A", "receiver-B", None, "INR", "2026-01-01 10:00:00"])
    buf = io.BytesIO()
    workbook.save(buf)
    data = buf.getvalue()

    assess_schema(FINANCIAL_TRANSACTION_GENERIC_V1, peek_xlsx_header(data))
    results = [
        normalize_chunk(FINANCIAL_TRANSACTION_GENERIC_V1, chunk)
        for chunk in iter_xlsx_record_chunks(data, batch_size=10)
    ]
    malformed = [row for result in results for row in result.malformed_rows]
    assert len(malformed) == 1
    assert malformed[0].locator.sheet == "Txns"
