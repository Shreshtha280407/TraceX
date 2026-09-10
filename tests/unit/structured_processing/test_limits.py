"""Scenario 10 (general): documented safety limits are actually enforced."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import validate_zip_container
from app.modules.structured_processing.models import TextSegment


def _make_zip(entry_count: int, entry_size: int = 10) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for i in range(entry_count):
            archive.writestr(f"entry-{i}.txt", "x" * entry_size)
    return buf.getvalue()


def test_valid_zip_container_passes() -> None:
    validate_zip_container(_make_zip(3), error_code=ErrorCode.INVALID_DOCX)


def test_zip_entry_count_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.structured_processing.limits as limits_module

    monkeypatch.setattr(limits_module, "MAX_ZIP_ENTRIES", 2)
    with pytest.raises(ProcessingError) as exc_info:
        validate_zip_container(_make_zip(5), error_code=ErrorCode.INVALID_XLSX)
    assert exc_info.value.code == ErrorCode.INVALID_XLSX


def test_zip_uncompressed_size_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.structured_processing.limits as limits_module

    monkeypatch.setattr(limits_module, "MAX_ZIP_UNCOMPRESSED_BYTES", 5)
    with pytest.raises(ProcessingError) as exc_info:
        validate_zip_container(_make_zip(1, entry_size=1000), error_code=ErrorCode.INVALID_DOCX)
    assert exc_info.value.code == ErrorCode.INVALID_DOCX


def test_zip_compression_ratio_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.structured_processing.limits as limits_module

    # A highly-compressible payload gives a large uncompressed/compressed ratio.
    monkeypatch.setattr(limits_module, "MAX_ZIP_COMPRESSION_RATIO", 2)
    zip_bytes = _make_zip(1, entry_size=100_000)
    with pytest.raises(ProcessingError) as exc_info:
        validate_zip_container(zip_bytes, error_code=ErrorCode.INVALID_XLSX)
    assert exc_info.value.code == ErrorCode.INVALID_XLSX


def test_not_a_zip_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        validate_zip_container(b"definitely not a zip file", error_code=ErrorCode.INVALID_DOCX)
    assert exc_info.value.code == ErrorCode.INVALID_DOCX


def test_pdf_page_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.structured_processing.document.pdf as pdf_module
    from tests.fixtures.structured_processing.builders import build_minimal_pdf

    monkeypatch.setattr(pdf_module, "MAX_PDF_PAGES", 1)
    pdf_bytes = build_minimal_pdf(["page one", "page two"])
    with pytest.raises(ProcessingError) as exc_info:
        pdf_module.extract_pdf(pdf_bytes)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_extracted_text_length_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.structured_processing.document.text_extractors as text_extractors_module

    monkeypatch.setattr(text_extractors_module, "MAX_TEXT_LENGTH", 5)
    with pytest.raises(ProcessingError) as exc_info:
        text_extractors_module.enforce_text_limits([TextSegment(page=None, text="way too long")])
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED
