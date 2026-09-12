"""Safe, structured processing errors.

`ProcessingError` is the only exception this module's parsing/validation
code should raise for an expected, named failure mode — mirroring
`app.modules.structured_processing.errors` exactly. `worker.py` catches
exactly this type and turns it into `WorkerResultV1(status=FAILED,
error=WorkerError(...))`, never a raw exception message, stack trace, or
source content (message, handle, phone number, token). Anything that
isn't a `ProcessingError` is a programming bug and is deliberately *not*
caught here.

A deferral (real ASR/diarization needed, an unsupported-but-not-invalid
audio format) is a normal, expected outcome — not a failure — and is
represented as a `WorkerResultV1(status=DEFERRED)` return value, never an
exception. See `audio/routing.py`.

The four errors at the bottom of this file (`WorkerAuthenticationError`,
`InputResolutionUnavailableError`, `WorkerApiError`, and their common base
`WorkerOrchestrationError`) are a distinct category: *orchestration*-level
failures talking to Nipun's internal worker API (claim/submit/input
resolution) via `client.py`/`worker.run_once` — not source-parsing
failures. Mirrors `app.modules.structured_processing.errors`'s identical
addition exactly. None of these four may ever be constructed with a worker
token, a claim token, or a raw HTTP response body in the message, for the
same reason `ProcessingError.message` may never carry source content.
"""

from __future__ import annotations


class ErrorCode:
    """Stable, documented error codes for `WorkerResultV1.error.code`."""

    UNSUPPORTED_PROCESSOR = "unsupported_processor"
    UNSUPPORTED_SOURCE_TYPE = "unsupported_source_type"
    INPUT_ROLE_MISMATCH = "input_role_mismatch"
    INVALID_WAV = "invalid_wav"
    UNSUPPORTED_AUDIO_FORMAT = "unsupported_audio_format"
    INPUT_LIMIT_EXCEEDED = "input_limit_exceeded"
    INVALID_TRANSCRIPT_SEGMENT = "invalid_transcript_segment"
    INVALID_DIARIZATION_SEGMENT = "invalid_diarization_segment"
    MALFORMED_CHAT_EXPORT = "malformed_chat_export"
    UNSUPPORTED_CHAT_FORMAT = "unsupported_chat_format"
    AMBIGUOUS_TIMEZONE = "ambiguous_timezone"
    CROSS_CASE_INPUT_REJECTED = "cross_case_input_rejected"
    REQUIRED_FIELD_MISSING = "required_field_missing"
    #: A transcript/diarization interchange payload (see
    #: `audio/transcript_import.py::parse_transcript_import_payload`,
    #: `audio/diarization_import.py::parse_diarization_import_payload`) is
    #: not valid UTF-8, not valid JSON, not the documented top-level shape
    #: (`{"segments": [...]}`), or a segment object is missing a required
    #: field / has a field of the wrong type -- before any timing/bounds
    #: validation ever runs.
    MALFORMED_JSON_PAYLOAD = "malformed_json_payload"
    #: Raised only if `AsrAdapter.transcribe`/`DiarizationAdapter.diarize` is
    #: ever called directly while `state` reports `UNAVAILABLE` -- a
    #: programming-error safety net, not a path `worker.py`'s live dispatch
    #: can reach today (it defers via `AudioRoutingDecision.DEFERRED_*`
    #: *before* any adapter would be invoked). See `audio/asr_adapter.py`/
    #: `audio/diarization_adapter.py`.
    ASR_ADAPTER_UNAVAILABLE = "asr_adapter_unavailable"
    DIARIZATION_ADAPTER_UNAVAILABLE = "diarization_adapter_unavailable"


class ProcessingError(Exception):
    """A named, expected processing failure with a safe (non-secret) message.

    `message` must never contain message text, handles, phone numbers,
    file paths, timestamps as submitted, or raw exception text from a
    third-party library — only a description of *what kind* of problem
    occurred (e.g. "segment 4 has end_ms <= start_ms"), never the
    offending value itself.
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
    """The internal worker API rejected the worker token or a claim token.

    Never constructed with the token itself, or with the API's raw
    response body (which could itself echo request details).
    """


class InputResolutionUnavailableError(WorkerOrchestrationError):
    """The claimed job's evidence bytes could not be retrieved through the approved endpoint.

    Raised by `input_resolver.LiveInputResolver` on a `404` from
    `client.WorkerApiClient.fetch_input` -- an honest, loud signal that the
    input-access endpoint is unreachable (e.g. an older API build), never
    silently bypassed with a direct storage read.
    """


class WorkerApiError(WorkerOrchestrationError):
    """An unexpected (non-auth, non-4xx-validation) failure calling the internal worker API.

    Never constructed with the raw response body or the underlying HTTP
    client exception's text -- only a safe description (e.g. "claim
    request failed: HTTP 503").
    """
