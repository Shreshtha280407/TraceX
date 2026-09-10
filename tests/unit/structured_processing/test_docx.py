"""Scenario 5: DOCX paragraph/table extraction preserves deterministic spans."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.document.docx import extract_docx
from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from tests.fixtures.structured_processing.builders import build_docx


def test_docx_extraction_produces_single_segment_with_no_page() -> None:
    segments = extract_docx(build_docx(["FIR No: 21/2026", "Reported by phone 9876543210"]))
    assert len(segments) == 1
    assert segments[0].page is None
    assert "FIR No: 21/2026" in segments[0].text
    assert "9876543210" in segments[0].text


def test_docx_table_cells_are_included() -> None:
    segments = extract_docx(
        build_docx(
            paragraphs=["Case summary"],
            table_rows=[["Field", "Value"], ["FIR No", "99/2026"]],
        )
    )
    combined = segments[0].text
    assert "Case summary" in combined
    assert "99/2026" in combined


def test_docx_empty_paragraphs_and_cells_are_skipped() -> None:
    segments = extract_docx(build_docx(["", "Real content", "  "]))
    assert segments[0].text == "Real content"


def test_docx_extraction_is_deterministic() -> None:
    data = build_docx(["Deterministic paragraph one.", "Deterministic paragraph two."])
    first = extract_docx(data)
    second = extract_docx(data)
    assert first == second


def test_docx_spans_are_exact_offsets_into_extracted_text() -> None:
    segments = extract_docx(build_docx(["Preamble text.", "FIR No: 66/2026 details follow."]))
    mentions = extract_fir_mentions(segments)
    fir_mentions = [m for m in mentions if m.observation_type == "fir_reference"]
    assert len(fir_mentions) == 1
    span_start = fir_mentions[0].locator.span_start
    span_end = fir_mentions[0].locator.span_end
    assert span_start is not None and span_end is not None
    assert segments[0].text[span_start:span_end] == fir_mentions[0].text


def test_invalid_docx_bytes_fail_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        extract_docx(b"not a real docx file")
    assert exc_info.value.code == ErrorCode.INVALID_DOCX
