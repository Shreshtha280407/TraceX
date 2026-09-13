"""Shared raster-image OCR bounding-box adapter (Phase 3 — Gaurav).

The **single, named entry point** for all image/video-frame OCR inside the
TraceX media-processing pipeline. Future modules (document-page rasterisation
is Jasraj's domain and is explicitly out of scope here) consume this module
rather than calling ``TesseractTextRecognizer`` directly, so coordinate-
transform semantics, provenance metadata, and safety rules are enforced once.

Design contract
---------------
1. **Original-coordinate guarantee.** ``OcrBoxResult.pixel_box`` and
   ``bbox_xyxy_normalized`` always refer to the *original source
   image/frame* that was passed to ``ImageOcrAdapter``, not to any
   internally-resized, cropped, or orientation-corrected intermediate. If
   preprocessing is applied (EXIF orientation correction is the only default
   transform) the adapter records the transform in ``OcrPreprocessTransform``
   and inverts it before populating coordinates in the result.

2. **No fabricated text on failure.** When the local ``tesseract`` binary or
   requested language pack is unavailable the adapter *raises*
   ``OcrRuntimeError`` at construction time (same as
   ``TesseractTextRecognizer.__post_init__``). It never silently substitutes
   placeholder text as OCR output. A corrupt/unreadable image raises
   ``ProcessingError(MEDIA_DECODE_FAILED)`` and returns no results.

3. **Bounded confidence.** ``OcrBoxResult.confidence`` is always in ``[0,
   1]`` -- Tesseract's ``0–100`` scale is divided by 100 exactly as
   ``TesseractTextRecognizer.recognize_regions`` already does. Values outside
   that range or non-finite values are rejected (``is_valid_confidence``).

4. **No cloud / no automatic downloads.** The adapter is a thin wrapper over
   the system ``tesseract`` binary; it never calls a cloud API or downloads
   a model. See ``docs/architecture/image-ocr-provenance.md``.

5. **Fixture / real separation.** ``FixtureOcrAdapter`` is the only adapter
   whose text contains the literal prefix ``FIXTURE_OCR_`` -- any downstream
   code that needs to distinguish fixture from real output may check for this
   prefix. The real ``ImageOcrAdapter`` is never constructed during unit tests
   that do not have the system tesseract binary installed.

Supported raster inputs (via ``image/decoder.py``'s Pillow backend):
  - PNG
  - JPEG / JPG

PDF page rasterisation is Jasraj's structured-processing domain; do not add
it here.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.contracts.common import BoundingBoxNormalized
from app.modules.media_processing.analysis.interfaces import TextRecognizer
from app.modules.media_processing.analysis.tesseract_ocr import (
    TESSERACT_OCR_VERSION,
    TesseractOcrConfig,
    TesseractTextRecognizer,
)
from app.modules.media_processing.errors import ProcessingError
from app.modules.media_processing.image.decoder import decode_image
from app.modules.media_processing.image.geometry import PixelBoundingBox, to_normalized
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS, MediaLimits
from app.modules.media_processing.models import Frame, ImageMetadata
from app.modules.media_processing.provenance import is_valid_confidence

logger = structlog.get_logger(__name__)

#: Version identifier for the adapter's preprocessing/coordinate-transform
#: semantics. Bump this string whenever coordinate-mapping logic changes --
#: it flows into ``OcrBoxResult.preprocessing_version`` and into
#: ``TransformationProvenanceV1.safe_metadata`` so downstream code and
#: replays can detect a semantics change.
OCR_ADAPTER_PREPROCESSING_VERSION = "media_ocr_adapter_v1"

#: Observation-type constant used when building ``ObservationV1`` from
#: an ``OcrBoxResult``.  Mirrors ``provenance.OBSERVATION_OCR_TEXT_MENTION``
#: (same value) and is re-exported from here so callers of this adapter
#: never need to import ``provenance`` for this one constant.
OBSERVATION_TYPE_OCR_TEXT = "ocr_text_mention"

#: Prefix that every ``FixtureOcrAdapter`` result text starts with. Downstream
#: code may use this as a sentinel to detect fixture vs real OCR output.
FIXTURE_TEXT_PREFIX = "FIXTURE_OCR_"


# ---------------------------------------------------------------------------
# Typed configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrAdapterConfig:
    """Typed, explicit configuration for ``ImageOcrAdapter``.

    All defaults are conservative and safe for arbitrary natural photos /
    video frames -- not tuned for structured-document OCR (that's
    ``document_ocr_*`` settings in ``Settings``).
    """

    #: Tesseract language pack name.  The adapter fails at construction
    #: time (``OcrRuntimeError``) if this pack isn't installed -- it never
    #: falls back to a different language silently.
    language: str = "eng"
    #: Minimum per-line confidence in ``[0, 1]``.  Lines below this floor
    #: are dropped before result objects are returned.
    min_confidence: float = 0.3
    #: Tesseract PSM.  11 = "sparse text" -- suitable for arbitrary photos.
    page_segmentation_mode: int = 11
    #: When ``True`` the adapter corrects EXIF orientation before passing
    #: the image to the OCR engine and maps resulting pixel coordinates back
    #: to the original (un-rotated) image's coordinate space.
    correct_exif_orientation: bool = True
    #: Optional override of the ``tesseract`` binary path -- ``None`` uses
    #: ``pytesseract``'s own ``PATH`` lookup.
    tesseract_cmd: str | None = None


def ocr_config_hash(config: OcrAdapterConfig) -> str:
    """A short, deterministic hex hash of an ``OcrAdapterConfig``.

    Used in ``TransformationProvenanceV1.config_hash`` and
    ``OcrBoxResult.config_hash`` so downstream replay can detect when the
    OCR configuration changed between two runs.
    """
    canonical = (
        f"lang={config.language}"
        f"|min_conf={config.min_confidence:.6f}"
        f"|psm={config.page_segmentation_mode}"
        f"|orient={config.correct_exif_orientation}"
        f"|cmd={config.tesseract_cmd or ''}"
        f"|preprocess={OCR_ADAPTER_PREPROCESSING_VERSION}"
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Preprocessing transform record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrPreprocessTransform:
    """Records what preprocessing was applied before OCR.

    Captured in every ``OcrBoxResult`` so a consumer can reconstruct the
    exact pixel-coordinate mapping that was used.

    Current supported transforms (applied in this order):
      1. EXIF orientation correction (rotation/flip to upright).

    Future transforms (resize, denoise, contrast) must be added here with
    their own fields and documented inversion formulas.
    """

    #: EXIF orientation value found in the source image, or ``None`` if
    #: EXIF was absent or unreadable.  Value is the raw EXIF tag integer
    #: (1 = no rotation, 3 = 180°, 6 = 90° CW, 8 = 90° CCW, etc.).
    #: ``None`` means no orientation correction was applied.
    applied_exif_orientation: int | None = None

    @property
    def is_identity(self) -> bool:
        """True when no transform was applied (or only orientation=1, which is a no-op)."""
        return self.applied_exif_orientation is None or self.applied_exif_orientation == 1


# ---------------------------------------------------------------------------
# Typed OCR result
# ---------------------------------------------------------------------------


OcrUnit = Literal["word", "line", "block"]


@dataclass(frozen=True)
class OcrBoxResult:
    """One OCR-recognized text region in a raster image or video frame.

    **All coordinates refer to the original source image**, not any
    preprocessed intermediate. See ``OCR_ADAPTER_PREPROCESSING_VERSION`` and
    ``OcrPreprocessTransform`` for the documented mapping semantics.

    This is the canonical output type of ``ImageOcrAdapter`` and
    ``FixtureOcrAdapter``.  Downstream batch-building code
    (``ocr_batching.py``) consumes this type directly to produce
    ``ObservationV1`` and ``TransformationProvenanceV1`` objects.
    """

    #: The recognized text.  Never the full image or a raw OCR dump.
    #: Bounded: ``TesseractTextRecognizer.recognize_regions`` already
    #: joins at most one line's worth of words.
    text: str
    #: Extraction quality in ``[0, 1]``.  This is OCR signal quality,
    #: never a probability of guilt or evidentiary truth.
    confidence: float
    #: Pixel bbox in the **original** source image's coordinate space.
    pixel_box: PixelBoundingBox
    #: Normalized ``[0, 1]`` bbox derived from ``pixel_box`` and the
    #: original source dimensions. Validated by ``BoundingBoxNormalized``
    #: (``x_min < x_max``, ``y_min < y_max``, all in ``[0, 1]``).
    bbox_xyxy_normalized: BoundingBoxNormalized
    #: Width of the **original** source image (before any preprocessing).
    source_image_width: int
    #: Height of the **original** source image.
    source_image_height: int
    #: Granularity of this result. ``TesseractTextRecognizer`` returns line-
    #: level results; the adapter preserves that.
    ocr_unit: OcrUnit = "line"
    #: OCR engine identifier string (e.g. ``"tesseract_ocr_v1"``).
    ocr_engine_name: str = TESSERACT_OCR_VERSION
    #: The Tesseract version string reported by the installed binary, or
    #: ``"unavailable"`` for a ``FixtureOcrAdapter``.
    ocr_engine_version: str = "unknown"
    #: Language/configuration hash of the adapter that produced this result.
    language: str = "eng"
    #: Deterministic hash of the ``OcrAdapterConfig`` used.
    config_hash: str = ""
    #: Version string of the preprocessing/coordinate-transform semantics.
    preprocessing_version: str = OCR_ADAPTER_PREPROCESSING_VERSION
    #: What preprocessing was applied (for coordinate inversion audit).
    preprocess_transform: OcrPreprocessTransform = field(default_factory=OcrPreprocessTransform)


# ---------------------------------------------------------------------------
# Coordinate-inversion helpers
# ---------------------------------------------------------------------------


def _invert_exif_orientation_box(
    box: PixelBoundingBox,
    *,
    preprocessed_width: int,
    preprocessed_height: int,
    original_width: int,
    original_height: int,
    orientation: int,
) -> PixelBoundingBox:
    """Map a pixel bbox from the *preprocessed* (orientation-corrected) image
    back to the *original* (as-stored) image's coordinate space.

    The EXIF orientation transforms and their inverses follow the standard
    EXIF 2.3 spec matrix operations.  Only the orientations that
    ``decode_image`` (Pillow's ``ImageOps.exif_transpose``) actually applies
    are handled; orientation 1 (no-op) and unknown values fall through to the
    identity transform.

    Arguments:
        box:                Pixel bbox in the preprocessed image's space.
        preprocessed_width: Width of the preprocessed (transposed) image.
        preprocessed_height: Height of the preprocessed image.
        original_width:     Width of the original (un-transposed) image.
        original_height:    Height of the original image.
        orientation:        Raw EXIF orientation tag integer.

    Returns:
        Pixel bbox in the original image's coordinate space.
    """
    x0, y0, x1, y1 = box.x_min, box.y_min, box.x_max, box.y_max

    # Pillow's `ImageOps.exif_transpose` performs the following operations:
    # 1 → identity
    # 2 → flip horizontal
    # 3 → rotate 180°
    # 4 → flip vertical
    # 5 → transpose (flip diagonal)
    # 6 → rotate 90° CCW (= rotate 270° CW) in original → flip back = rotate 90° CW
    # 7 → transverse
    # 8 → rotate 90° CW (= rotate 270° CCW) in original → flip back = rotate 90° CCW
    #
    # We are given coords in the *output* of that transform and need to
    # recover coords in the *input* (original) space.  The inverses are:
    # 1 → identity (inverse of identity)
    # 2 → flip horizontal (self-inverse)
    # 3 → rotate 180° (self-inverse)
    # 4 → flip vertical (self-inverse)
    # 5 → transpose (self-inverse)
    # 6 → rotate 90° CCW (inverse of the CCW rotation Pillow applies → rotate 90° CW)
    # 7 → transverse (self-inverse)
    # 8 → rotate 90° CW (inverse of the CW rotation Pillow applies → rotate 90° CCW)
    #
    # preprocessed dimensions were already swapped by Pillow for orientations
    # 5-8 (width/height are exchanged after the transpose).

    pw, ph = preprocessed_width, preprocessed_height  # noqa: F841 — used below

    if orientation == 2:
        # flip horizontal: x' = W - x
        x0_new = original_width - x1
        x1_new = original_width - x0
        return PixelBoundingBox(x_min=x0_new, y_min=y0, x_max=x1_new, y_max=y1)

    elif orientation == 3:
        # rotate 180°: (x', y') = (W - x, H - y)
        x0_new = original_width - x1
        y0_new = original_height - y1
        x1_new = original_width - x0
        y1_new = original_height - y0
        return PixelBoundingBox(x_min=x0_new, y_min=y0_new, x_max=x1_new, y_max=y1_new)

    elif orientation == 4:
        # flip vertical: y' = H - y
        y0_new = original_height - y1
        y1_new = original_height - y0
        return PixelBoundingBox(x_min=x0, y_min=y0_new, x_max=x1, y_max=y1_new)

    elif orientation == 5:
        # transpose (flip along main diagonal): (x', y') = (y, x)
        # Inverse of transpose is transpose itself.
        # preprocessed (x, y) → original (y, x); dims were swapped.
        return PixelBoundingBox(x_min=y0, y_min=x0, x_max=y1, y_max=x1)

    elif orientation == 6:
        # Pillow rotates 90° CCW (counter-clockwise) for EXIF orientation 6.
        # In the preprocessed (rotated CCW) space: (x, y) → original: (y, H_orig - x)
        # original_width = preprocessed_height (dims swapped), original_height = preprocessed_width
        # (x_orig, y_orig) = (y_pre, original_height - x_pre)
        x0_new = y0
        y0_new = original_height - x1
        x1_new = y1
        y1_new = original_height - x0
        return PixelBoundingBox(
            x_min=min(x0_new, x1_new),
            y_min=min(y0_new, y1_new),
            x_max=max(x0_new, x1_new),
            y_max=max(y0_new, y1_new),
        )

    elif orientation == 7:
        # transverse (flip + rotate 90°): (x', y') = (H - y, W - x)
        x0_new = original_width - y1
        y0_new = original_height - x1
        x1_new = original_width - y0
        y1_new = original_height - x0
        return PixelBoundingBox(
            x_min=min(x0_new, x1_new),
            y_min=min(y0_new, y1_new),
            x_max=max(x0_new, x1_new),
            y_max=max(y0_new, y1_new),
        )

    elif orientation == 8:
        # Pillow rotates 90° CW for EXIF orientation 8.
        # In the preprocessed (rotated CW) space: (x, y) → original: (original_width - y, x)
        x0_new = original_width - y1
        y0_new = x0
        x1_new = original_width - y0
        y1_new = x1
        return PixelBoundingBox(
            x_min=min(x0_new, x1_new),
            y_min=min(y0_new, y1_new),
            x_max=max(x0_new, x1_new),
            y_max=max(y0_new, y1_new),
        )

    # orientation 1 or unrecognized → identity
    return box


# ---------------------------------------------------------------------------
# Real OCR adapter
# ---------------------------------------------------------------------------


@dataclass
class ImageOcrAdapter:
    """Shared raster-image OCR adapter backed by the local Tesseract binary.

    This is the **only** way media-processing pipeline code should run OCR on
    a standalone image or a selected video frame.  It wraps
    ``TesseractTextRecognizer`` while:

    * capturing the OCR engine version at construction time,
    * applying (and recording) the EXIF orientation correction that
      ``decode_image`` already performs, so bbox coordinates map back to
      the original image's coordinate space, and
    * producing typed ``OcrBoxResult`` objects instead of the lower-level
      ``RecognizedText`` protocol type.

    Construction raises ``OcrRuntimeError`` when the ``tesseract`` binary or
    the requested language pack is unavailable -- never silently substitutes
    a different engine or fabricates output.
    """

    config: OcrAdapterConfig = field(default_factory=OcrAdapterConfig)

    def __post_init__(self) -> None:
        # Raises OcrRuntimeError if tesseract is missing / language unavailable.
        self._recognizer: TextRecognizer = TesseractTextRecognizer(
            config=TesseractOcrConfig(
                language=self.config.language,
                min_confidence=self.config.min_confidence,
                page_segmentation_mode=self.config.page_segmentation_mode,
                tesseract_cmd=self.config.tesseract_cmd,
            )
        )
        import pytesseract

        try:
            raw_version = str(pytesseract.get_tesseract_version())
        except Exception:  # noqa: BLE001
            raw_version = "unknown"
        self._engine_version = raw_version
        self._config_hash = ocr_config_hash(self.config)

    @property
    def engine_version(self) -> str:
        """Tesseract version string reported by the installed binary."""
        return self._engine_version

    @property
    def config_hash(self) -> str:
        """Deterministic hash of this adapter's ``OcrAdapterConfig``."""
        return self._config_hash

    def run(self, image: Frame, metadata: ImageMetadata) -> list[OcrBoxResult]:
        """Run OCR on a decoded image ``Frame``.

        ``metadata`` carries the **original** source dimensions and the EXIF
        orientation value from ``decode_image``.  The adapter uses these to
        map Tesseract's output bboxes (which are in the
        post-``exif_transpose`` space) back to the original pixel space.

        Returns an empty list when no text is found or the image is empty.
        All returned boxes have been validated by ``to_normalized``; any
        geometrically invalid box is silently dropped (same policy as
        ``_ocr_region_observation_image`` in ``worker.py``).
        """
        if image.shape[0] <= 0 or image.shape[1] <= 0:
            return []

        # The Frame passed here has already been orientation-corrected by
        # decode_image (Pillow's exif_transpose).  Tesseract's output
        # coordinates are in the *corrected* frame's space.
        preprocessed_height, preprocessed_width = int(image.shape[0]), int(image.shape[1])

        # Original (pre-correction) dimensions from the ImageMetadata.
        original_width = metadata.width
        original_height = metadata.height

        orientation = metadata.orientation  # None or raw EXIF int

        preprocess_transform = OcrPreprocessTransform(
            applied_exif_orientation=orientation
            if (self.config.correct_exif_orientation and orientation not in (None, 1))
            else None
        )

        raw_regions = self._recognizer.recognize_regions(image)

        results: list[OcrBoxResult] = []
        for region in raw_regions:
            if not is_valid_confidence(region.confidence):
                continue

            # Map the bbox back to original image coordinate space.
            if preprocess_transform.is_identity:
                original_box = region.box
            else:
                assert orientation is not None  # guaranteed by is_identity check
                original_box = _invert_exif_orientation_box(
                    region.box,
                    preprocessed_width=preprocessed_width,
                    preprocessed_height=preprocessed_height,
                    original_width=original_width,
                    original_height=original_height,
                    orientation=orientation,
                )

            # Validate and normalize against original dimensions.
            try:
                normalized = to_normalized(
                    original_box,
                    image_width=original_width,
                    image_height=original_height,
                )
            except ProcessingError:
                # Geometrically invalid box -- skip, same policy as worker.py.
                logger.debug(
                    "ocr_adapter.invalid_box_skipped",
                    x_min=original_box.x_min,
                    y_min=original_box.y_min,
                    x_max=original_box.x_max,
                    y_max=original_box.y_max,
                )
                continue

            results.append(
                OcrBoxResult(
                    text=region.text,
                    confidence=region.confidence,
                    pixel_box=original_box,
                    bbox_xyxy_normalized=normalized,
                    source_image_width=original_width,
                    source_image_height=original_height,
                    ocr_unit="line",
                    ocr_engine_name=TESSERACT_OCR_VERSION,
                    ocr_engine_version=self._engine_version,
                    language=self.config.language,
                    config_hash=self._config_hash,
                    preprocessing_version=OCR_ADAPTER_PREPROCESSING_VERSION,
                    preprocess_transform=preprocess_transform,
                )
            )

        logger.debug(
            "ocr_adapter.run_complete",
            region_count=len(results),
            original_width=original_width,
            original_height=original_height,
        )
        return results

    def run_on_bytes(
        self,
        data: bytes,
        *,
        limits: MediaLimits = DEFAULT_MEDIA_LIMITS,
    ) -> list[OcrBoxResult]:
        """Decode raw image bytes and run OCR.

        Raises ``ProcessingError(MEDIA_DECODE_FAILED)`` for unidentifiable or
        corrupt image data, or ``ProcessingError(MEDIA_LIMIT_EXCEEDED)`` for
        dimensions outside ``limits``.  Never fabricates output for a bad input.

        Supported formats: whatever Pillow's ``Image.open`` accepts for raster
        images (PNG and JPEG are the tested, required formats -- see
        ``docs/architecture/image-ocr-provenance.md``).
        """
        image, metadata = decode_image(data, limits=limits)
        return self.run(image, metadata)


# ---------------------------------------------------------------------------
# Fixture-only adapter (test-safe, clearly labelled)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureOcrAdapter:
    """Deterministic, test-only OCR adapter.

    Output text always starts with ``FIXTURE_OCR_`` so downstream code
    can unconditionally distinguish fixture from real OCR results.  Like
    ``FakeTextRecognizer``, output depends *only* on image dimensions, never
    on pixel content.  Must never be used in production or live tests in
    place of the real ``ImageOcrAdapter``.
    """

    confidence: float = 0.75
    language: str = "fixture-only"

    @property
    def config_hash(self) -> str:
        return "fixture-config-hash"

    @property
    def engine_version(self) -> str:
        return "fixture-engine"

    def run(self, image: Frame, metadata: ImageMetadata) -> list[OcrBoxResult]:
        """Return exactly one result covering the whole image, labelled FIXTURE_OCR_."""
        w, h = metadata.width, metadata.height
        if w <= 0 or h <= 0:
            return []
        pixel_box = PixelBoundingBox(
            x_min=10.0, y_min=10.0, x_max=float(w - 10), y_max=float(h - 10)
        )
        # Validate that the fixture box is geometrically sensible.
        if pixel_box.x_max <= pixel_box.x_min or pixel_box.y_max <= pixel_box.y_min:
            pixel_box = PixelBoundingBox(x_min=0.0, y_min=0.0, x_max=float(w), y_max=float(h))
        try:
            normalized = to_normalized(pixel_box, image_width=w, image_height=h)
        except ProcessingError:
            return []
        conf = (
            self.confidence
            if math.isfinite(self.confidence) and 0.0 <= self.confidence <= 1.0
            else 0.5
        )
        return [
            OcrBoxResult(
                text=f"{FIXTURE_TEXT_PREFIX}{w}x{h}",
                confidence=conf,
                pixel_box=pixel_box,
                bbox_xyxy_normalized=normalized,
                source_image_width=w,
                source_image_height=h,
                ocr_unit="line",
                ocr_engine_name="fixture_ocr",
                ocr_engine_version="fixture-engine",
                language=self.language,
                config_hash=self.config_hash,
                preprocessing_version=OCR_ADAPTER_PREPROCESSING_VERSION,
                preprocess_transform=OcrPreprocessTransform(),
            )
        ]

    def run_on_bytes(
        self,
        data: bytes,
        *,
        limits: MediaLimits = DEFAULT_MEDIA_LIMITS,
    ) -> list[OcrBoxResult]:
        """Decode bytes (real decode -- corrupt input still fails safely) then run fixture OCR."""
        image, metadata = decode_image(data, limits=limits)
        return self.run(image, metadata)


__all__ = [
    "FIXTURE_TEXT_PREFIX",
    "OBSERVATION_TYPE_OCR_TEXT",
    "OCR_ADAPTER_PREPROCESSING_VERSION",
    "TESSERACT_OCR_VERSION",
    "FixtureOcrAdapter",
    "ImageOcrAdapter",
    "OcrAdapterConfig",
    "OcrBoxResult",
    "OcrPreprocessTransform",
    "OcrUnit",
    "ocr_config_hash",
]
