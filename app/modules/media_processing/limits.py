"""Documented, enforced input-safety limits for media processing.

Every limit here exists to stop a hostile or malformed video/image from
exhausting memory, CPU, or subprocess time before expensive decode work
happens: a claimed-4K image that is actually a decompression bomb, a video
with an absurd duration/frame-rate, or a runaway `ffprobe`/`ffmpeg`
invocation. Limits are conservative constants with safe development
defaults, not configurable per-request -- mirrors
`app.modules.structured_processing.limits`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.models import ImageMetadata, VideoMetadata


@dataclass(frozen=True)
class MediaLimits:
    """Typed, immutable input-safety limits for one processing run."""

    max_input_bytes: int
    max_video_duration_ms: int
    max_width: int
    max_height: int
    max_frame_rate: float
    max_sampled_frames: int
    max_image_pixels: int
    max_ocr_crop_count: int
    subprocess_timeout_seconds: float


#: Safe defaults for local development and CI. Deliberately conservative:
#: Phase 1 processes investigative evidence offline, not user-tunable
#: uploads, so there is no need to make these request-configurable yet.
DEFAULT_MEDIA_LIMITS = MediaLimits(
    max_input_bytes=500 * 1024 * 1024,  # 500 MiB per source file
    max_video_duration_ms=2 * 60 * 60 * 1000,  # 2 hours
    max_width=7680,  # 8K wide
    max_height=4320,  # 8K tall
    max_frame_rate=120.0,
    max_sampled_frames=500,
    max_image_pixels=64_000_000,  # ~64 MP; well below a classic decompression-bomb size
    max_ocr_crop_count=200,
    subprocess_timeout_seconds=30.0,
)


def check_input_size(num_bytes: int, limits: MediaLimits) -> None:
    """Reject an over-limit source before it is written to disk or decoded."""
    if num_bytes > limits.max_input_bytes:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"input exceeds the {limits.max_input_bytes}-byte limit",
        )


def check_video_limits(metadata: VideoMetadata, limits: MediaLimits) -> None:
    """Reject an over-limit video before sampling/frame extraction begins."""
    if metadata.duration_ms > limits.max_video_duration_ms:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"video duration exceeds the {limits.max_video_duration_ms}ms limit",
        )
    if metadata.width > limits.max_width or metadata.height > limits.max_height:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"video dimensions exceed the {limits.max_width}x{limits.max_height} limit",
        )
    if metadata.frame_rate is not None and metadata.frame_rate > limits.max_frame_rate:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"video frame rate exceeds the {limits.max_frame_rate}fps limit",
        )


def check_image_pixel_dimensions(width: int, height: int, limits: MediaLimits) -> None:
    """Reject decompression-bomb-like image dimensions before pixel decode.

    Callers must invoke this *before* asking a decoder to materialize
    pixels -- image dimensions are readable from a lazily-opened image's
    header alone, so this check never itself triggers a full decode.
    """
    if width > limits.max_width or height > limits.max_height:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"image dimensions exceed the {limits.max_width}x{limits.max_height} limit",
        )
    if width * height > limits.max_image_pixels:
        raise ProcessingError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"image pixel count exceeds the {limits.max_image_pixels} limit",
        )


def check_image_limits(metadata: ImageMetadata, limits: MediaLimits) -> None:
    """Reject an over-limit image, given already-extracted metadata."""
    check_image_pixel_dimensions(metadata.width, metadata.height, limits)
