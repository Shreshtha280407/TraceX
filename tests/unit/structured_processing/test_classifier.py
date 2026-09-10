"""Content classification: content-type + extension cross-checked, never trusted alone."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.document.classifier import ContentKind, classify
from app.modules.structured_processing.errors import ErrorCode, ProcessingError


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("application/pdf", "report.pdf", ContentKind.PDF),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "report.docx",
            ContentKind.DOCX,
        ),
        ("text/plain", "notes.txt", ContentKind.TXT),
        ("text/csv", "records.csv", ContentKind.CSV),
        ("application/json", "records.json", ContentKind.JSON),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "records.xlsx",
            ContentKind.XLSX,
        ),
    ],
)
def test_classify_supported_types(content_type: str, filename: str, expected: ContentKind) -> None:
    assert classify(content_type, filename) is expected


def test_classify_rejects_unsupported_content_type() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        classify("application/zip", "archive.zip")
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


def test_classify_rejects_mismatched_extension() -> None:
    """A renamed file (real PDF bytes, `.txt` name) must not be trusted on extension alone."""
    with pytest.raises(ProcessingError) as exc_info:
        classify("application/pdf", "report.txt")
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_EXTENSION


def test_classify_rejects_missing_extension() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        classify("text/csv", "records")
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_EXTENSION
