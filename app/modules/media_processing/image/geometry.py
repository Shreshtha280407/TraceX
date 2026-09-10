"""Pixel-to-normalized bounding-box conversion.

A future detector emits pixel coordinates; every observation this module
emits must carry `bbox_xyxy_normalized` (see `app.contracts.common.
BoundingBoxNormalized`). Conversion never clamps an invalid box into range
-- an out-of-range, degenerate, or non-finite box is rejected outright, per
`docs/architecture/media-processing-v1.md`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import ValidationError

from app.contracts.common import BoundingBoxNormalized
from app.modules.media_processing.errors import ErrorCode, ProcessingError


@dataclass(frozen=True)
class PixelBoundingBox:
    """An axis-aligned box in pixel coordinates, as a detector would emit it."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float


def to_normalized(
    box: PixelBoundingBox, *, image_width: int, image_height: int
) -> BoundingBoxNormalized:
    """Convert a pixel-space box to `[0, 1]`-normalized coordinates.

    Raises `ProcessingError(INVALID_BOUNDING_BOX)` for a non-finite value,
    non-positive image dimensions, or geometry that fails
    `BoundingBoxNormalized`'s own `x_min < x_max` / `y_min < y_max` /
    `[0, 1]` validation -- never clamps.
    """
    values = (box.x_min, box.y_min, box.x_max, box.y_max)
    if not all(math.isfinite(v) for v in values):
        raise ProcessingError(
            ErrorCode.INVALID_BOUNDING_BOX, "bounding box contains a non-finite value"
        )
    if image_width <= 0 or image_height <= 0:
        raise ProcessingError(
            ErrorCode.INVALID_BOUNDING_BOX,
            "image dimensions must be positive to normalize a bounding box",
        )
    try:
        return BoundingBoxNormalized(
            x_min=box.x_min / image_width,
            y_min=box.y_min / image_height,
            x_max=box.x_max / image_width,
            y_max=box.y_max / image_height,
        )
    except ValidationError as exc:
        raise ProcessingError(
            ErrorCode.INVALID_BOUNDING_BOX, "bounding box geometry is invalid"
        ) from exc
