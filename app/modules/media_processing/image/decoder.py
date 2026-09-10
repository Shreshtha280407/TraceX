"""Safe image decoding via Pillow.

Dimensions are read from the image header (`Image.open` is lazy) and
checked against `MediaLimits` *before* any pixel data is decoded --
rejecting a decompression-bomb-like image never requires actually decoding
its pixels. Only then is the image converted to RGB and materialized as a
`Frame` array, the same pixel-data shape video frame extraction produces.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, UnidentifiedImageError

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.limits import MediaLimits, check_image_pixel_dimensions
from app.modules.media_processing.models import Frame, ImageMetadata

#: EXIF Orientation tag (0x0112 / 274), per the EXIF 2.3 specification.
_EXIF_ORIENTATION_TAG = 0x0112


def _safe_orientation(image: Image.Image) -> int | None:
    try:
        value = image.getexif().get(_EXIF_ORIENTATION_TAG)
    except Exception:  # noqa: BLE001 - EXIF reading is best-effort and must never crash decode
        return None
    return value if isinstance(value, int) else None


def decode_image(data: bytes, *, limits: MediaLimits) -> tuple[Frame, ImageMetadata]:
    """Decode image bytes to an RGB `Frame` plus its safe metadata.

    Raises `ProcessingError(MEDIA_DECODE_FAILED)` for unidentifiable or
    corrupt image data, or `ProcessingError(MEDIA_LIMIT_EXCEEDED)` for
    dimensions outside `limits` -- checked before pixel decode.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image_format = image.format or "unknown"
        width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ProcessingError(
            ErrorCode.MEDIA_DECODE_FAILED, "image could not be identified"
        ) from exc

    check_image_pixel_dimensions(width, height, limits)

    try:
        color_mode = image.mode
        orientation = _safe_orientation(image)
        rgb_image = image.convert("RGB")
        array: Frame = np.asarray(rgb_image, dtype=np.uint8)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ProcessingError(ErrorCode.MEDIA_DECODE_FAILED, "image could not be decoded") from exc

    metadata = ImageMetadata(
        width=width,
        height=height,
        format=image_format,
        color_mode=color_mode,
        orientation=orientation,
    )
    return array, metadata
