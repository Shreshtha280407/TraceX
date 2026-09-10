"""Deterministic video frame-sampling plans.

Sampling policy (see `docs/architecture/media-processing-v1.md` for the
full writeup): given the same `VideoMetadata` and the same
`SamplingRequest`, `build_sample_plan` always returns the same, deduplicated,
ascending list of `SampleTimestamp`s. No strategy here is adaptive or
content-aware -- that is explicitly out of scope for this phase.
"""

from __future__ import annotations

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.models import (
    SampleTimestamp,
    SamplingRequest,
    SamplingStrategy,
    VideoMetadata,
)


def _uniform_timestamps(duration_ms: int, interval_ms: int) -> list[int]:
    if interval_ms <= 0:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED, "sampling interval_ms must be a positive integer"
        )
    timestamps = list(range(0, max(duration_ms, 1), interval_ms))
    return timestamps or [0]


def _fixed_fps_timestamps(duration_ms: int, fps: float) -> list[int]:
    if fps <= 0:
        raise ProcessingError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "sampling fps must be positive")
    interval_ms = max(int(round(1000.0 / fps)), 1)
    return _uniform_timestamps(duration_ms, interval_ms)


def build_sample_plan(metadata: VideoMetadata, request: SamplingRequest) -> list[SampleTimestamp]:
    """Build a deterministic, deduplicated, ascending sample plan.

    Raises `ProcessingError(SAMPLING_LIMIT_EXCEEDED)` if the strategy would
    produce more than `request.max_frames` timestamps -- this module never
    silently truncates a sample plan.
    """
    if request.strategy is SamplingStrategy.UNIFORM_INTERVAL:
        if request.interval_ms is None:
            raise ProcessingError(
                ErrorCode.MEDIA_LIMIT_EXCEEDED, "uniform_interval sampling requires interval_ms"
            )
        raw_timestamps = _uniform_timestamps(metadata.duration_ms, request.interval_ms)
    elif request.strategy is SamplingStrategy.FIXED_FPS:
        if request.fps is None:
            raise ProcessingError(ErrorCode.MEDIA_LIMIT_EXCEEDED, "fixed_fps sampling requires fps")
        raw_timestamps = _fixed_fps_timestamps(metadata.duration_ms, request.fps)
    elif request.strategy is SamplingStrategy.EXPLICIT_TIMESTAMPS:
        raw_timestamps = list(request.explicit_timestamps_ms)
    else:  # pragma: no cover - exhaustive StrEnum
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED, f"unknown sampling strategy '{request.strategy}'"
        )

    deduplicated = sorted(set(raw_timestamps))
    if len(deduplicated) > request.max_frames:
        raise ProcessingError(
            ErrorCode.SAMPLING_LIMIT_EXCEEDED,
            f"sampling plan of {len(deduplicated)} frames exceeds the "
            f"{request.max_frames}-frame limit",
        )
    return [SampleTimestamp(time_ms=time_ms, index=i) for i, time_ms in enumerate(deduplicated)]
