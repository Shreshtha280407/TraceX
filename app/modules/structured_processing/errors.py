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

The three errors at the bottom of this file (`WorkerAuthenticationError`,
`InputResolutionUnavailableError`, `WorkerApiError`) are a distinct
category: *orchestration*-level failures talking to Nipun's internal
worker API (claim/submit/input-resolution), not source-parsing failures.
`worker.py`'s CLI runner catches these separately from `ProcessingError` --
see its module docstring. None of these three may ever be constructed with
`WORKER_TOKEN`, a claim token, or a raw HTTP response body in the
message, for the same reason `ProcessingError.message` may never carry
extracted source content.
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
    OCR_RUNTIME_UNAVAILABLE = "ocr_runtime_unavailable"
    NER_RUNTIME_UNAVAILABLE = "ner_runtime_unavailable"
    AMBIGUOUS_SCHEMA = "ambiguous_schema"
    PARTIAL_ROW_FAILURES = "partial_row_failures"


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


class WorkerOrchestrationError(Exception):
    """Base class for the worker CLI's own (non-parsing) safe, named failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class WorkerAuthenticationError(WorkerOrchestrationError):
    """The internal worker API rejected the shared secret or a claim token.

    Never constructed with the secret/token itself, or with the API's raw
    response body (which could itself echo request details).
    """


class InputResolutionUnavailableError(WorkerOrchestrationError):
    """The approved way to fetch a claimed job's evidence bytes/metadata is not available.

    Raised by `input_resolver.LiveInputResolver` when Nipun's internal API
    has no route matching the documented, proposed input-access endpoint
    (see `docs/architecture/structured-processing-worker.md`, "Input-access
    boundary") -- this is an honest, loud signal that the integration seam
    is missing, never silently bypassed with direct MinIO access.
    """


class WorkerApiError(WorkerOrchestrationError):
    """An unexpected (non-auth, non-4xx-validation) failure calling the internal worker API.

    Never constructed with the raw response body or the underlying HTTP
    client exception's text -- only a safe description (e.g. "claim
    request failed: HTTP 503").
    """
