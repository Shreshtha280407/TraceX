"""Safe, structured processing errors.

`ProcessingError` is the only exception parser code should raise for an
expected, named failure mode. `worker.py` catches exactly this type and
turns it into a `WorkerResultV1(status=FAILED, error=WorkerError(...))` —
never a raw exception message, stack trace, or source content. Anything
that isn't a `ProcessingError` is a programming bug, not an input problem,
and is deliberately *not* caught here (see `worker.py`).

OCR deferral (`document_requires_ocr`) is a normal, expected outcome for a
scanned PDF, not a failure — it is represented as a `WorkerResultV1(status=
DEFERRED)` return value, not an exception. See `document/ocr_routing.py`.
"""

from __future__ import annotations


class ErrorCode:
    """Stable, documented error codes for `WorkerResultV1.error.code`."""

    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    MALFORMED_CSV = "malformed_csv"
    MALFORMED_JSON = "malformed_json"
    INVALID_XLSX = "invalid_xlsx"
    INVALID_DOCX = "invalid_docx"
    INVALID_PDF = "invalid_pdf"
    ENCRYPTED_PDF_UNSUPPORTED = "encrypted_pdf_unsupported"
    DOCUMENT_REQUIRES_OCR = "document_requires_ocr"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
    REQUIRED_FIELD_MISSING = "required_field_missing"
    UNSUPPORTED_PARSER_PROFILE = "unsupported_parser_profile"


class ProcessingError(Exception):
    """A named, expected processing failure with a safe (non-secret) message.

    `message` must never contain extracted source content, credentials, or
    raw exception text from a third-party library — only a description of
    *what kind* of problem occurred (e.g. "row 12 missing required field
    'caller_number'"), never the offending value itself.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
