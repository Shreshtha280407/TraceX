"""Safe `ffmpeg` frame extraction: sample timestamps in, decoded frames out.

Each timestamp is extracted independently via a raw-pixel `ffmpeg` pipe (no
per-frame temp files, no shell interpolation -- argument arrays only). A
single failed timestamp (out of range, corrupt region) is skipped and
counted rather than failing the whole batch; only a missing `ffmpeg` binary
aborts the batch outright. `frame_number` is populated only when the
video's frame count/frame rate were both reliably probed -- otherwise the
timestamp-to-frame mapping is not trustworthy, and `frame_number` is left
`None` rather than guessed (see `docs/architecture/media-processing-v1.md`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.limits import MediaLimits
from app.modules.media_processing.models import ExtractedFrame, SampleTimestamp, VideoMetadata


def _trustworthy_frame_number(metadata: VideoMetadata, time_ms: int) -> int | None:
    if metadata.frame_count is None or metadata.frame_rate is None:
        return None
    computed = int(round((time_ms / 1000.0) * metadata.frame_rate))
    return max(0, min(computed, metadata.frame_count - 1))


def _frame_duration_ms(metadata: VideoMetadata) -> int:
    if metadata.frame_rate is None or metadata.frame_rate <= 0:
        return 0
    return max(int(round(1000.0 / metadata.frame_rate)), 1)


def _extract_one(
    path: Path, metadata: VideoMetadata, timestamp: SampleTimestamp, limits: MediaLimits
) -> ExtractedFrame | None:
    expected_bytes = metadata.width * metadata.height * 3
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, path is a local temp file
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-ss",
                f"{timestamp.time_ms / 1000.0:.6f}",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            timeout=limits.subprocess_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0 or len(completed.stdout) != expected_bytes:
        return None

    image = np.frombuffer(completed.stdout, dtype=np.uint8).reshape(
        (metadata.height, metadata.width, 3)
    )
    duration = _frame_duration_ms(metadata)
    return ExtractedFrame(
        frame_number=_trustworthy_frame_number(metadata, timestamp.time_ms),
        time_start_ms=timestamp.time_ms,
        time_end_ms=timestamp.time_ms + duration,
        width=metadata.width,
        height=metadata.height,
        image=image,
    )


def extract_frames(
    path: Path,
    metadata: VideoMetadata,
    timestamps: list[SampleTimestamp],
    *,
    limits: MediaLimits,
) -> tuple[list[ExtractedFrame], int]:
    """Extract each requested timestamp; returns `(frames, frames_failed)`.

    Raises `ProcessingError(FFMPEG_UNAVAILABLE)` up front if `ffmpeg` is not
    installed at all -- everything after that is a per-frame soft failure,
    not a hard error, matching real-world video (a handful of unreadable
    frames should not discard an otherwise-usable sample).
    """
    if shutil.which("ffmpeg") is None:
        raise ProcessingError(ErrorCode.FFMPEG_UNAVAILABLE, "ffmpeg is not available")

    frames: list[ExtractedFrame] = []
    failed = 0
    for timestamp in timestamps:
        frame = _extract_one(path, metadata, timestamp, limits)
        if frame is None:
            failed += 1
        else:
            frames.append(frame)
    return frames, failed
