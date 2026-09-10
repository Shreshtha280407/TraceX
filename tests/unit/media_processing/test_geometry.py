"""Scenarios 7-8: pixel-to-normalized bounding-box conversion; never clamps."""

from __future__ import annotations

import math

import pytest

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.image.geometry import PixelBoundingBox, to_normalized


def test_to_normalized_converts_pixel_box_correctly() -> None:
    box = PixelBoundingBox(x_min=10, y_min=20, x_max=90, y_max=180)
    normalized = to_normalized(box, image_width=100, image_height=200)
    assert normalized.x_min == pytest.approx(0.1)
    assert normalized.y_min == pytest.approx(0.1)
    assert normalized.x_max == pytest.approx(0.9)
    assert normalized.y_max == pytest.approx(0.9)


def test_to_normalized_rejects_non_finite_values() -> None:
    box = PixelBoundingBox(x_min=0, y_min=0, x_max=math.inf, y_max=10)
    with pytest.raises(ProcessingError) as excinfo:
        to_normalized(box, image_width=100, image_height=100)
    assert excinfo.value.code == ErrorCode.INVALID_BOUNDING_BOX


def test_to_normalized_rejects_nan() -> None:
    box = PixelBoundingBox(x_min=0, y_min=0, x_max=math.nan, y_max=10)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_rejects_degenerate_box_without_clamping() -> None:
    # x_min == x_max: zero-width box, invalid per BoundingBoxNormalized's own rule.
    box = PixelBoundingBox(x_min=50, y_min=0, x_max=50, y_max=10)
    with pytest.raises(ProcessingError) as excinfo:
        to_normalized(box, image_width=100, image_height=100)
    assert excinfo.value.code == ErrorCode.INVALID_BOUNDING_BOX


def test_to_normalized_rejects_inverted_box_without_clamping() -> None:
    box = PixelBoundingBox(x_min=90, y_min=0, x_max=10, y_max=10)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_rejects_out_of_range_box_without_clamping() -> None:
    # x_max beyond image_width -> normalized x_max > 1, must be rejected, not clamped to 1.
    box = PixelBoundingBox(x_min=0, y_min=0, x_max=150, y_max=10)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_rejects_negative_pixel_coordinates() -> None:
    box = PixelBoundingBox(x_min=-10, y_min=0, x_max=50, y_max=10)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_rejects_nonpositive_image_dimensions() -> None:
    box = PixelBoundingBox(x_min=0, y_min=0, x_max=10, y_max=10)
    with pytest.raises(ProcessingError) as excinfo:
        to_normalized(box, image_width=0, image_height=100)
    assert excinfo.value.code == ErrorCode.INVALID_BOUNDING_BOX
