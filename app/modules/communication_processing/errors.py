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
