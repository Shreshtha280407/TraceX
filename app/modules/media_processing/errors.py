"""Safe, structured media-processing errors.

`ProcessingError` is the only exception media pipeline code should raise for
an expected, named failure mode. `worker.py` catches exactly this type and
turns it into a `WorkerResultV1(status=FAILED, error=WorkerError(...))` --
never a raw exception message, subprocess output, local filesystem path, or
media content. Anything that isn't a `ProcessingError` is a programming bug,
not an input problem, and is deliberately *not* caught here (see
`worker.py`), mirroring `app.modules.structured_processing.errors`.
"""

from __future__ import annotations


class ErrorCode:
    """Stable, documented error codes for `WorkerResultV1.error.code`."""

    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    MEDIA_PROBE_FAILED = "media_probe_failed"
    MEDIA_DECODE_FAILED = "media_decode_failed"
    MEDIA_LIMIT_EXCEEDED = "media_limit_exceeded"
    FFPROBE_UNAVAILABLE = "ffprobe_unavailable"
    FFMPEG_UNAVAILABLE = "ffmpeg_unavailable"
    INVALID_BOUNDING_BOX = "invalid_bounding_box"
    SAMPLING_LIMIT_EXCEEDED = "sampling_limit_exceeded"
    ANALYSIS_NOT_CONFIGURED = "analysis_not_configured"


class ProcessingError(Exception):
    """A named, expected media-processing failure with a safe (non-secret) message.

    `message` must never contain a local filesystem path, raw subprocess
    stdout/stderr, raw frame/image bytes, or other media content -- only a
    description of *what kind* of problem occurred.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
