"""Safe, structured media-processing errors.

`ProcessingError` is the only exception media pipeline code should raise for
an expected, named failure mode. `worker.py` catches exactly this type and
turns it into a `WorkerResultV1(status=FAILED, error=WorkerError(...))` --
never a raw exception message, subprocess output, local filesystem path, or
media content. Anything that isn't a `ProcessingError` is a programming bug,
not an input problem, and is deliberately *not* caught here (see
`worker.py`), mirroring `app.modules.structured_processing.errors`.

The four errors at the bottom of this file (`WorkerAuthenticationError`,
`InputResolutionUnavailableError`, `WorkerApiError`, and their common base
`WorkerOrchestrationError`) are a distinct category: *orchestration*-level
failures talking to Nipun's internal worker API (claim/submit/input
resolution) via `client.py`/`worker.run_once` -- not media-parsing
failures. Mirrors `app.modules.communication_processing.errors`'s identical
addition exactly. None of these four may ever be constructed with a worker
token, a claim token, or a raw HTTP response body in the message, for the
same reason `ProcessingError.message` may never carry media content.
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
    #: A configured local model asset (detector weights) is missing,
    #: unreadable, or fails its pinned SHA-256 verification -- see
    #: `analysis/onnx_detector.py` and `bootstrap_models.py`. Never raised
    #: mid-job for a *transient* reason; this is always a deployment/
    #: configuration problem, caught at component construction time.
    MODEL_ASSET_UNAVAILABLE = "model_asset_unavailable"
    #: The local OCR runtime (the `tesseract` binary, or a requested
    #: language pack) is missing or not usable -- see
    #: `analysis/tesseract_ocr.py`.
    OCR_RUNTIME_UNAVAILABLE = "ocr_runtime_unavailable"


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


class ModelAssetError(WorkerOrchestrationError):
    """A configured local detector model asset is missing, unreadable, or fails checksum
    verification -- a startup/configuration problem, not a per-job outcome.

    Raised by `_build_analysis_components` (`worker.py`) before any job is
    ever claimed, so a misconfigured deployment fails loudly and immediately
    (CLI exit `1`) rather than claiming and failing every detection job one
    at a time. Never constructed with the local filesystem path -- only a
    safe description of what's wrong (missing / checksum mismatch / unreadable).
    """


class OcrRuntimeError(WorkerOrchestrationError):
    """The local OCR runtime (the `tesseract` binary, or a requested language pack) is
    missing or unusable -- a startup/configuration problem, not a per-job outcome.

    Never constructed with raw subprocess output.
    """
