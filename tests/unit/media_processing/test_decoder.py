"""Scenario 6: image decoding and decompression-bomb dimension rejection."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.image.decoder import decode_image
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS, MediaLimits
from tests.fixtures.media_processing.synthetic import make_png_bytes


def test_decode_image_returns_frame_and_metadata() -> None:
    data = make_png_bytes(width=32, height=16, color=(1, 2, 3))
    frame, metadata = decode_image(data, limits=DEFAULT_MEDIA_LIMITS)
    assert frame.shape == (16, 32, 3)
    assert metadata.width == 32
    assert metadata.height == 16
    assert metadata.format == "PNG"
    assert metadata.color_mode == "RGB"


def test_decode_image_rejects_unidentifiable_data() -> None:
    with pytest.raises(ProcessingError) as excinfo:
        decode_image(b"not an image at all", limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_DECODE_FAILED


def test_decode_image_rejects_truncated_data() -> None:
    data = make_png_bytes(width=64, height=64)
    truncated = data[: len(data) // 2]
    with pytest.raises(ProcessingError) as excinfo:
        decode_image(truncated, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_DECODE_FAILED


def test_decode_image_rejects_decompression_bomb_like_dimensions_before_decode() -> None:
    tiny_limits = MediaLimits(
        max_input_bytes=DEFAULT_MEDIA_LIMITS.max_input_bytes,
        max_video_duration_ms=DEFAULT_MEDIA_LIMITS.max_video_duration_ms,
        max_width=10,
        max_height=10,
        max_frame_rate=DEFAULT_MEDIA_LIMITS.max_frame_rate,
        max_sampled_frames=DEFAULT_MEDIA_LIMITS.max_sampled_frames,
        max_image_pixels=100,
        max_ocr_crop_count=DEFAULT_MEDIA_LIMITS.max_ocr_crop_count,
        subprocess_timeout_seconds=DEFAULT_MEDIA_LIMITS.subprocess_timeout_seconds,
    )
    data = make_png_bytes(width=64, height=64)
    with pytest.raises(ProcessingError) as excinfo:
        decode_image(data, limits=tiny_limits)
    assert excinfo.value.code == ErrorCode.MEDIA_LIMIT_EXCEEDED


def test_decode_image_reads_exif_orientation_when_present() -> None:
    image = Image.new("RGB", (8, 8), color=(5, 5, 5))
    exif = image.getexif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    _frame, metadata = decode_image(buffer.getvalue(), limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.orientation == 6


def test_decode_image_orientation_is_none_when_absent() -> None:
    data = make_png_bytes(width=8, height=8)
    _frame, metadata = decode_image(data, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.orientation is None
