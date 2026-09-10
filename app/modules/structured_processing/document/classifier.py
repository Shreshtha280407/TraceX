"""Safe content classification for every Phase 1 supported format.

Classification uses `EvidenceRecordV1.content_type` *and* the filename
extension as two independent, cross-checked signals — never the extension
alone (a renamed file shouldn't be trusted) and never the declared
content-type alone (a mislabeled upload shouldn't be trusted either). A
mismatch between the two is rejected rather than guessed at.
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.structured_processing.errors import ErrorCode, ProcessingError


class ContentKind(StrEnum):
    """The processing path a supported source file takes."""

    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"


_CONTENT_TYPE_TO_KIND: dict[str, ContentKind] = {
    "application/pdf": ContentKind.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ContentKind.DOCX,
    "text/plain": ContentKind.TXT,
    "text/csv": ContentKind.CSV,
    "application/json": ContentKind.JSON,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ContentKind.XLSX,
}

_KIND_TO_EXTENSIONS: dict[ContentKind, frozenset[str]] = {
    ContentKind.PDF: frozenset({"pdf"}),
    ContentKind.DOCX: frozenset({"docx"}),
    ContentKind.TXT: frozenset({"txt"}),
    ContentKind.CSV: frozenset({"csv"}),
    ContentKind.XLSX: frozenset({"xlsx"}),
    ContentKind.JSON: frozenset({"json"}),
}


def _extension_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def classify(content_type: str, filename: str) -> ContentKind:
    """Classify a source by content type, cross-checked against its filename extension.

    Raises `ProcessingError(UNSUPPORTED_CONTENT_TYPE)` for a content type
    outside the Phase 1 supported set, or `ProcessingError
    (UNSUPPORTED_EXTENSION)` when the filename extension doesn't match what
    that content type requires.
    """
    kind = _CONTENT_TYPE_TO_KIND.get(content_type.lower().strip())
    if kind is None:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_CONTENT_TYPE,
            f"content type '{content_type}' is not supported in this phase",
        )

    extension = _extension_of(filename)
    if extension not in _KIND_TO_EXTENSIONS[kind]:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_EXTENSION,
            f"filename extension '.{extension}' is inconsistent with content type '{content_type}'",
        )

    return kind
