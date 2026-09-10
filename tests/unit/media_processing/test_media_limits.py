"""Scenario 2: media size/dimension/frame-rate/duration limit enforcement."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.limits import (
    DEFAULT_MEDIA_LIMITS,
    check_image_limits,
    check_image_pixel_dimensions,
    check_input_size,
    check_video_limits,
)
from app.modules.media_processing.models import ImageMetadata, VideoMetadata

_VIDEO = VideoMetadata(
    container_format="mov,mp4,m4a",
    duration_ms=5000,
    width=640,
    height=480,
    frame_rate=30.0,
    frame_count=150,
    video_codec="h264",
    has_audio=False,
    rotation_degrees=None,
)


def test_check_input_size_accepts_within_limit() -> None:
    check_input_size(1024, DEFAULT_MEDIA_LIMITS)


def test_check_input_size_rejects_over_limit() -> None:
    with pytest.raises(ProcessingError) as excinfo:
        check_input_size(DEFAULT_MEDIA_LIMITS.max_input_bytes + 1, DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_LIMIT_EXCEEDED


def test_check_video_limits_accepts_within_limits() -> None:
    check_video_limits(_VIDEO, DEFAULT_MEDIA_LIMITS)


def test_check_video_limits_rejects_over_duration() -> None:
    over = replace(_VIDEO, duration_ms=DEFAULT_MEDIA_LIMITS.max_video_duration_ms + 1)
    with pytest.raises(ProcessingError) as excinfo:
        check_video_limits(over, DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_LIMIT_EXCEEDED


def test_check_video_limits_rejects_over_dimensions() -> None:
    over = replace(_VIDEO, width=DEFAULT_MEDIA_LIMITS.max_width + 1)
    with pytest.raises(ProcessingError):
        check_video_limits(over, DEFAULT_MEDIA_LIMITS)


def test_check_video_limits_rejects_over_frame_rate() -> None:
    over = replace(_VIDEO, frame_rate=DEFAULT_MEDIA_LIMITS.max_frame_rate + 1)
    with pytest.raises(ProcessingError):
        check_video_limits(over, DEFAULT_MEDIA_LIMITS)


def test_check_video_limits_allows_unknown_frame_rate() -> None:
    unknown_rate = replace(_VIDEO, frame_rate=None)
    check_video_limits(unknown_rate, DEFAULT_MEDIA_LIMITS)


def test_check_image_pixel_dimensions_rejects_decompression_bomb_like_size() -> None:
    with pytest.raises(ProcessingError) as excinfo:
        check_image_pixel_dimensions(20000, 20000, DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_LIMIT_EXCEEDED


def test_check_image_pixel_dimensions_accepts_reasonable_size() -> None:
    check_image_pixel_dimensions(1920, 1080, DEFAULT_MEDIA_LIMITS)


def test_check_image_limits_delegates_to_pixel_dimension_check() -> None:
    huge = ImageMetadata(
        width=20000, height=20000, format="PNG", color_mode="RGB", orientation=None
    )
    with pytest.raises(ProcessingError):
        check_image_limits(huge, DEFAULT_MEDIA_LIMITS)
