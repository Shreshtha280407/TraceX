"""Scenarios 2-4: native PDF extraction, scanned-page OCR deferral, encrypted PDF rejection."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.document.ocr_routing import route_pdf
from app.modules.structured_processing.document.pdf import extract_pdf
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from tests.fixtures.structured_processing.builders import build_encrypted_pdf, build_minimal_pdf


def test_native_text_pdf_extracts_page_provenance() -> None:
    pdf_bytes = build_minimal_pdf(
        ["FIR No: 42/2026 reported at station", "Second page content, also substantial enough"]
    )
    result = extract_pdf(pdf_bytes)

    assert result.total_pages == 2
    assert result.ocr_required_pages == []
    assert [segment.page for segment in result.segments] == [1, 2]
    assert "FIR No: 42/2026" in result.segments[0].text
    assert "Second page" in result.segments[1].text


def test_scanned_only_pdf_defers_and_fabricates_nothing() -> None:
    pdf_bytes = build_minimal_pdf([None, None])
    result = extract_pdf(pdf_bytes)

    assert result.segments == []
    assert result.ocr_required_pages == [1, 2]

    routing = route_pdf(result)
    assert routing.requires_ocr is True
    assert routing.fully_requires_ocr is True
    assert routing.deferred_pages == (1, 2)
    assert routing.checkpoint is not None
    assert ErrorCode.DOCUMENT_REQUIRES_OCR in routing.checkpoint


def test_partially_scanned_pdf_keeps_real_pages_and_defers_the_rest() -> None:
    pdf_bytes = build_minimal_pdf(["Real embedded text on page one, quite substantial.", None])
    result = extract_pdf(pdf_bytes)

    assert [s.page for s in result.segments] == [1]
    assert result.ocr_required_pages == [2]

    routing = route_pdf(result)
    assert routing.requires_ocr is True
    assert routing.fully_requires_ocr is False
    assert routing.deferred_pages == (2,)


def test_encrypted_pdf_is_rejected_without_password_guessing() -> None:
    pdf_bytes = build_encrypted_pdf(password="secret123")
    with pytest.raises(ProcessingError) as exc_info:
        extract_pdf(pdf_bytes)
    assert exc_info.value.code == ErrorCode.ENCRYPTED_PDF_UNSUPPORTED


def test_invalid_pdf_bytes_fail_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        extract_pdf(b"this is not a pdf at all")
    assert exc_info.value.code == ErrorCode.INVALID_PDF


def test_pdf_extraction_is_deterministic() -> None:
    pdf_bytes = build_minimal_pdf(["Deterministic content here for repeated extraction test."])
    first = extract_pdf(pdf_bytes)
    second = extract_pdf(pdf_bytes)
    assert first.segments == second.segments
    assert first.ocr_required_pages == second.ocr_required_pages
