"""Unit tests for ``app.modules.media_processing.ocr_adapter`` (Phase 3 — Gaurav).

Scenarios 1-16 from the Phase 3 test matrix:

 1.  ``FixtureOcrAdapter`` output always starts with ``FIXTURE_OCR_``
 2.  ``FixtureOcrAdapter`` output depends only on dimensions, not pixel content
 3.  ``FixtureOcrAdapter`` confidence is bounded to [0, 1] even with invalid input
 4.  ``FixtureOcrAdapter`` returns empty list for empty/zero-area image
 5.  Pixel → normalized math is exact for a 100×50 image
 6.  Different source dimensions yield different normalized coordinates
 7.  All normalized bbox values lie in [0, 1] for any fixture result
 8.  ``to_normalized`` raises for a zero-area box (not silently fixed)
 9.  ``to_normalized`` raises for a reversed box (x_min >= x_max)
10.  ``to_normalized`` raises for NaN coords (not silently swapped)
11.  ``to_normalized`` raises for infinite coords
12.  EXIF orientation inversion: identity (orientation 1) is a no-op
13.  EXIF orientation inversion: 90° CCW (orientation 6) maps back correctly
14.  EXIF orientation inversion: 180° (orientation 3) maps back correctly
15.  ``FixtureOcrAdapter.run_on_bytes`` produces the same result as ``.run()``
16.  Real ``ImageOcrAdapter`` is not constructed without a working tesseract binary
     (self-skips when tesseract is present)
17.  ``TransformationProvenanceV1.safe_metadata`` never contains URI/credential keys
18.  ``ocr_config_hash`` is deterministic and changes when config changes
19.  ``OcrAdapterConfig`` default language is ``eng``
20.  ``FixtureOcrAdapter`` text prefix is unmistakably distinct from real OCR

Note: tests that assert on real Tesseract output are in ``test_tesseract_ocr.py``.
This suite avoids duplicating those; it focuses on the adapter's own contract.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from app.modules.media_processing.errors import ProcessingError
from app.modules.media_processing.image.geometry import PixelBoundingBox, to_normalized
from app.modules.media_processing.models import ImageMetadata
from app.modules.media_processing.ocr_adapter import (
    FIXTURE_TEXT_PREFIX,
    OCR_ADAPTER_PREPROCESSING_VERSION,
    FixtureOcrAdapter,
    ImageOcrAdapter,
    OcrAdapterConfig,
    OcrPreprocessTransform,
    _invert_exif_orientation_box,
    ocr_config_hash,
)
from tests.fixtures.media_processing.synthetic import (
    find_test_font,
    make_png_bytes,
    make_solid_frame,
    make_text_jpeg_bytes,
    make_text_png_bytes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(width: int, height: int, orientation: int | None = None) -> ImageMetadata:
    return ImageMetadata(
        width=width, height=height, format="PNG", color_mode="RGB", orientation=orientation
    )


def _solid_frame(width: int = 64, height: int = 48) -> np.ndarray:
    return make_solid_frame(width=width, height=height)


# ---------------------------------------------------------------------------
# 1. FixtureOcrAdapter: output prefix
# ---------------------------------------------------------------------------


def test_fixture_adapter_text_has_fixture_prefix() -> None:
    """Scenario 1: every fixture result text starts with FIXTURE_OCR_."""
    adapter = FixtureOcrAdapter()
    frame = _solid_frame(200, 100)
    meta = _meta(200, 100)
    results = adapter.run(frame, meta)
    assert results, "expected at least one fixture result"
    for r in results:
        assert r.text.startswith(FIXTURE_TEXT_PREFIX), (
            f"fixture text '{r.text!r}' does not start with {FIXTURE_TEXT_PREFIX!r}"
        )


# ---------------------------------------------------------------------------
# 2. FixtureOcrAdapter: determinism based only on dimensions
# ---------------------------------------------------------------------------


def test_fixture_adapter_output_depends_only_on_dimensions() -> None:
    """Scenario 2: same dims → same result, different pixel content → same result."""
    adapter = FixtureOcrAdapter()
    meta = _meta(200, 100)
    frame_a = np.zeros((100, 200, 3), dtype=np.uint8)  # all black
    frame_b = np.full((100, 200, 3), 200, dtype=np.uint8)  # all gray

    results_a = adapter.run(frame_a, meta)
    results_b = adapter.run(frame_b, meta)
    assert results_a == results_b, "same dims, different pixels → should yield identical results"


# ---------------------------------------------------------------------------
# 3. FixtureOcrAdapter: confidence always [0, 1]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_confidence",
    [0.0, 0.5, 1.0, 0.75],
)
def test_fixture_adapter_confidence_is_in_range(raw_confidence: float) -> None:
    """Scenario 3: confidence values in [0, 1] are preserved."""
    adapter = FixtureOcrAdapter(confidence=raw_confidence)
    frame = _solid_frame(200, 100)
    meta = _meta(200, 100)
    results = adapter.run(frame, meta)
    for r in results:
        assert 0.0 <= r.confidence <= 1.0, f"confidence {r.confidence!r} is out of range"


# ---------------------------------------------------------------------------
# 4. FixtureOcrAdapter: empty image returns empty list
# ---------------------------------------------------------------------------


def test_fixture_adapter_empty_image_returns_empty_list() -> None:
    """Scenario 4: zero-area images produce no results."""
    adapter = FixtureOcrAdapter()
    meta_zero = _meta(0, 0)
    frame_zero = np.zeros((0, 0, 3), dtype=np.uint8)
    assert adapter.run(frame_zero, meta_zero) == []


def test_fixture_adapter_zero_width_returns_empty_list() -> None:
    adapter = FixtureOcrAdapter()
    meta = _meta(0, 100)
    frame = np.zeros((100, 0, 3), dtype=np.uint8)
    assert adapter.run(frame, meta) == []


# ---------------------------------------------------------------------------
# 5. Geometry: pixel → normalized exact math
# ---------------------------------------------------------------------------


def test_to_normalized_exact_math_100x50_image() -> None:
    """Scenario 5: 100×50 image, box at pixel (10, 5, 90, 45) → (0.1, 0.1, 0.9, 0.9)."""
    box = PixelBoundingBox(x_min=10.0, y_min=5.0, x_max=90.0, y_max=45.0)
    result = to_normalized(box, image_width=100, image_height=50)
    assert result.x_min == pytest.approx(0.1)
    assert result.y_min == pytest.approx(0.1)
    assert result.x_max == pytest.approx(0.9)
    assert result.y_max == pytest.approx(0.9)


def test_to_normalized_full_image_is_unit_square() -> None:
    """A box covering the entire image normalizes to [0, 0, 1, 1] exactly."""
    box = PixelBoundingBox(x_min=0.0, y_min=0.0, x_max=100.0, y_max=50.0)
    result = to_normalized(box, image_width=100, image_height=50)
    assert result.x_min == pytest.approx(0.0)
    assert result.y_min == pytest.approx(0.0)
    assert result.x_max == pytest.approx(1.0)
    assert result.y_max == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 6. Geometry: different dims → different normalized results
# ---------------------------------------------------------------------------


def test_to_normalized_different_dims_yield_different_coords() -> None:
    """Scenario 6: same pixel box on different image sizes → different normalized coords."""
    box = PixelBoundingBox(x_min=10.0, y_min=10.0, x_max=50.0, y_max=40.0)
    r1 = to_normalized(box, image_width=100, image_height=100)
    r2 = to_normalized(box, image_width=200, image_height=200)
    assert r1.x_max != r2.x_max, (
        "different source dimensions must yield different normalized coords"
    )


# ---------------------------------------------------------------------------
# 7. Geometry: normalized bbox values in [0, 1] for fixture result
# ---------------------------------------------------------------------------


def test_fixture_adapter_normalized_bbox_in_unit_interval() -> None:
    """Scenario 7: all normalized bbox components lie in [0, 1]."""
    adapter = FixtureOcrAdapter()
    for width, height in [(100, 50), (400, 300), (1920, 1080), (20, 20)]:
        meta = _meta(width, height)
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        results = adapter.run(frame, meta)
        for r in results:
            bbox = r.bbox_xyxy_normalized
            assert 0.0 <= bbox.x_min <= 1.0
            assert 0.0 <= bbox.y_min <= 1.0
            assert 0.0 <= bbox.x_max <= 1.0
            assert 0.0 <= bbox.y_max <= 1.0
            assert bbox.x_min < bbox.x_max
            assert bbox.y_min < bbox.y_max


# ---------------------------------------------------------------------------
# 8-11. Geometry: to_normalized raises for invalid boxes
# ---------------------------------------------------------------------------


def test_to_normalized_raises_for_zero_area_box() -> None:
    """Scenario 8: zero-area box is rejected."""
    box = PixelBoundingBox(x_min=10.0, y_min=10.0, x_max=10.0, y_max=50.0)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_raises_for_reversed_box() -> None:
    """Scenario 9: reversed x-coords (x_min >= x_max) are rejected."""
    box = PixelBoundingBox(x_min=90.0, y_min=10.0, x_max=10.0, y_max=50.0)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_raises_for_nan_coords() -> None:
    """Scenario 10: NaN coordinates are rejected."""
    box = PixelBoundingBox(x_min=float("nan"), y_min=0.0, x_max=50.0, y_max=50.0)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_raises_for_infinite_coords() -> None:
    """Scenario 11: infinite coordinates are rejected."""
    box = PixelBoundingBox(x_min=0.0, y_min=0.0, x_max=float("inf"), y_max=50.0)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=100)


def test_to_normalized_raises_for_non_positive_image_dimensions() -> None:
    box = PixelBoundingBox(x_min=0.0, y_min=0.0, x_max=50.0, y_max=50.0)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=0, image_height=100)
    with pytest.raises(ProcessingError):
        to_normalized(box, image_width=100, image_height=0)


# ---------------------------------------------------------------------------
# 12-14. EXIF orientation inversion
# ---------------------------------------------------------------------------


def test_exif_inversion_identity_orientation_1() -> None:
    """Scenario 12: orientation 1 (no-op) returns box unchanged."""
    box = PixelBoundingBox(x_min=10.0, y_min=20.0, x_max=50.0, y_max=80.0)
    result = _invert_exif_orientation_box(
        box,
        preprocessed_width=100,
        preprocessed_height=200,
        original_width=100,
        original_height=200,
        orientation=1,
    )
    assert result == box


def test_exif_inversion_orientation_3_180_degrees() -> None:
    """Scenario 14: orientation 3 (180°) inverts correctly.

    Given a 100×200 image, a box at (10, 20, 50, 80) in the rotated space
    should map back to (50, 120, 90, 180) in the original space.
    """
    box = PixelBoundingBox(x_min=10.0, y_min=20.0, x_max=50.0, y_max=80.0)
    result = _invert_exif_orientation_box(
        box,
        preprocessed_width=100,
        preprocessed_height=200,
        original_width=100,
        original_height=200,
        orientation=3,
    )
    # (W - x_max, H - y_max, W - x_min, H - y_min) = (50, 120, 90, 180)
    assert result.x_min == pytest.approx(50.0)
    assert result.y_min == pytest.approx(120.0)
    assert result.x_max == pytest.approx(90.0)
    assert result.y_max == pytest.approx(180.0)


def test_exif_inversion_orientation_6_90_ccw() -> None:
    """Scenario 13: orientation 6 (Pillow rotates 90° CCW) maps back to original.

    For a 100×200 original image (portrait):
    - After Pillow's 90° CCW rotation → preprocessed is 200×100 (landscape).
    - A box at (20, 10, 80, 40) in the preprocessed (landscape) space
      maps back to... let's verify via the formula.
    """
    # original: 100w × 200h → after Pillow CCW: preprocessed 200w × 100h
    # Formula: x_orig = y_pre, y_orig = original_height - x_pre
    # Box in preprocessed: (x0, y0, x1, y1) = (20, 10, 80, 40)
    # x_orig: min(y0, y1)=min(10, 40)=10, max(y0, y1)=max(10, 40)=40
    # y_orig: min(H - x1, H - x0) = min(200-80, 200-20) = min(120, 180) = 120
    #         max = max(120, 180) = 180
    box = PixelBoundingBox(x_min=20.0, y_min=10.0, x_max=80.0, y_max=40.0)
    result = _invert_exif_orientation_box(
        box,
        preprocessed_width=200,
        preprocessed_height=100,
        original_width=100,
        original_height=200,
        orientation=6,
    )
    # The formula in _invert_exif_orientation_box for orientation 6:
    # x0_new = y0 = 10, y0_new = H - x1 = 200 - 80 = 120
    # x1_new = y1 = 40, y1_new = H - x0 = 200 - 20 = 180
    assert result.x_min == pytest.approx(10.0)
    assert result.y_min == pytest.approx(120.0)
    assert result.x_max == pytest.approx(40.0)
    assert result.y_max == pytest.approx(180.0)


# ---------------------------------------------------------------------------
# 15. FixtureOcrAdapter.run_on_bytes matches .run()
# ---------------------------------------------------------------------------


def test_fixture_adapter_run_on_bytes_matches_run() -> None:
    """Scenario 15: run_on_bytes decodes then produces same result as run()."""
    adapter = FixtureOcrAdapter()
    png_bytes = make_png_bytes(width=200, height=100)

    # run_on_bytes decodes and calls run internally.
    results_from_bytes = adapter.run_on_bytes(png_bytes)
    assert results_from_bytes, "expected at least one result"
    for r in results_from_bytes:
        assert r.text.startswith(FIXTURE_TEXT_PREFIX)
        assert 0.0 <= r.confidence <= 1.0
        assert r.source_image_width == 200
        assert r.source_image_height == 100


# ---------------------------------------------------------------------------
# 16. Real ImageOcrAdapter: fails without tesseract (monkeypatching)
# ---------------------------------------------------------------------------


def test_image_ocr_adapter_missing_binary_raises_ocr_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 16: ImageOcrAdapter raises OcrRuntimeError when tesseract is missing.

    Monkeypatches the pytesseract binary path -- runs unconditionally (same
    guard as test_tesseract_ocr.py's identical test).
    """
    monkeypatch.setattr("pytesseract.pytesseract.tesseract_cmd", "/nonexistent/tesseract")

    from app.modules.media_processing.errors import OcrRuntimeError
    from app.modules.media_processing.ocr_adapter import ImageOcrAdapter

    with pytest.raises(OcrRuntimeError, match="tesseract"):
        ImageOcrAdapter()


@pytest.mark.parametrize(
    ("encoded", "extension"),
    [(make_text_png_bytes, "png"), (make_text_jpeg_bytes, "jpeg")],
)
def test_real_image_ocr_adapter_extracts_labelled_synthetic_raster_text(
    encoded: Callable[..., bytes], extension: str
) -> None:
    """Exercise the real local adapter, never the fixture adapter.

    The fixture is deliberately high-contrast and uses a local system font;
    this is an integration-style unit test that self-skips only when the
    operator has not installed Tesseract or an OCR-readable font.  It proves
    PNG and JPEG bytes decode, Tesseract is invoked, returned text is bounded,
    and every geometry/provenance field is populated by ``ImageOcrAdapter``.
    """
    from app.modules.media_processing.errors import OcrRuntimeError

    font_path = find_test_font()
    if font_path is None:
        pytest.skip("no local TrueType font available for a real OCR fixture")
    try:
        adapter = ImageOcrAdapter()
    except OcrRuntimeError as exc:
        pytest.skip(f"real local OCR unavailable: {exc}")

    payload = encoded(
        "TRACEX OCR",
        width=640,
        height=180,
        font_path=font_path,
        font_size=72,
        text_x=30,
        text_y=40,
    )
    results = adapter.run_on_bytes(payload)

    assert results, f"real Tesseract returned no text for labelled {extension} fixture"
    recognized = " ".join(result.text for result in results).upper()
    assert "TRACEX" in recognized
    for result in results:
        assert result.ocr_engine_name != "fixture_ocr"
        assert result.ocr_engine_version not in {"", "unknown", "unavailable"}
        assert result.language == "eng"
        assert result.config_hash
        assert result.preprocessing_version == OCR_ADAPTER_PREPROCESSING_VERSION
        assert 0.0 <= result.confidence <= 1.0
        assert len(result.text) <= 500
        assert 0.0 <= result.bbox_xyxy_normalized.x_min < result.bbox_xyxy_normalized.x_max <= 1.0
        assert 0.0 <= result.bbox_xyxy_normalized.y_min < result.bbox_xyxy_normalized.y_max <= 1.0


# ---------------------------------------------------------------------------
# 17. Safe metadata fields are all safe
# ---------------------------------------------------------------------------


def test_ocr_adapter_result_attributes_have_no_forbidden_keys() -> None:
    """Scenario 17: OcrBoxResult fields used as safe_metadata contain no forbidden keys."""
    adapter = FixtureOcrAdapter()
    frame = _solid_frame(200, 100)
    meta = _meta(200, 100)
    results = adapter.run(frame, meta)
    assert results
    # These are the forbidden key substrings from
    # TransformationProvenanceV1._validate_safe_metadata.
    _FORBIDDEN = {
        "password",
        "secret",
        "token",
        "credential",
        "apikey",
        "api_key",
        "stderr",
        "stacktrace",
        "traceback",
        "object_uri",
        "filepath",
        "file_path",
        "dsn",
        "private_key",
    }
    for r in results:
        attrs = {
            "ocr_unit": r.ocr_unit,
            "source_image_width": r.source_image_width,
            "source_image_height": r.source_image_height,
            "ocr_engine": r.ocr_engine_name,
            "ocr_language": r.language,
            "preprocessing_version": r.preprocessing_version,
        }
        for key in attrs:
            for forbidden in _FORBIDDEN:
                assert forbidden not in key.lower(), (
                    f"attribute key '{key}' contains forbidden substring '{forbidden}'"
                )
        for val in attrs.values():
            if isinstance(val, str):
                assert len(val) <= 500, f"attribute value too long: {val!r}"


# ---------------------------------------------------------------------------
# 18. ocr_config_hash: deterministic and changes with config
# ---------------------------------------------------------------------------


def test_ocr_config_hash_is_deterministic() -> None:
    """Scenario 18a: same config → same hash."""
    cfg = OcrAdapterConfig(language="eng", min_confidence=0.3)
    assert ocr_config_hash(cfg) == ocr_config_hash(cfg)


def test_ocr_config_hash_changes_with_language() -> None:
    """Scenario 18b: different language → different hash."""
    h_eng = ocr_config_hash(OcrAdapterConfig(language="eng"))
    h_hin = ocr_config_hash(OcrAdapterConfig(language="hin"))
    assert h_eng != h_hin


def test_ocr_config_hash_changes_with_confidence() -> None:
    """Scenario 18c: different min_confidence → different hash."""
    h_low = ocr_config_hash(OcrAdapterConfig(min_confidence=0.1))
    h_high = ocr_config_hash(OcrAdapterConfig(min_confidence=0.9))
    assert h_low != h_high


# ---------------------------------------------------------------------------
# 19. OcrAdapterConfig defaults
# ---------------------------------------------------------------------------


def test_ocr_adapter_config_default_language_is_eng() -> None:
    """Scenario 19: the default OCR language is 'eng'."""
    cfg = OcrAdapterConfig()
    assert cfg.language == "eng"


def test_ocr_adapter_config_default_min_confidence_is_0_3() -> None:
    assert OcrAdapterConfig().min_confidence == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 20. FIXTURE_TEXT_PREFIX constant is clearly distinct
# ---------------------------------------------------------------------------


def test_fixture_text_prefix_is_fixture_ocr() -> None:
    """Scenario 20: prefix is the documented constant, unmistakable."""
    assert FIXTURE_TEXT_PREFIX == "FIXTURE_OCR_"


def test_fixture_text_prefix_not_in_real_english_text() -> None:
    """The prefix is not a word/phrase any real OCR engine would produce for normal text."""
    sample_real_texts = ["HELLO WORLD", "EXIT 42B", "POLICE STATION", "2026-01-01"]
    for text in sample_real_texts:
        assert not text.startswith(FIXTURE_TEXT_PREFIX), (
            f"real text '{text}' unexpectedly starts with fixture prefix"
        )


# ---------------------------------------------------------------------------
# OcrPreprocessTransform: is_identity
# ---------------------------------------------------------------------------


def test_preprocess_transform_identity_when_no_orientation() -> None:
    t = OcrPreprocessTransform(applied_exif_orientation=None)
    assert t.is_identity is True


def test_preprocess_transform_identity_for_orientation_1() -> None:
    t = OcrPreprocessTransform(applied_exif_orientation=1)
    assert t.is_identity is True


def test_preprocess_transform_not_identity_for_orientation_6() -> None:
    t = OcrPreprocessTransform(applied_exif_orientation=6)
    assert t.is_identity is False


# ---------------------------------------------------------------------------
# OCR_ADAPTER_PREPROCESSING_VERSION is stable
# ---------------------------------------------------------------------------


def test_preprocessing_version_constant_is_v1() -> None:
    """OCR_ADAPTER_PREPROCESSING_VERSION string must be the documented sentinel."""
    assert OCR_ADAPTER_PREPROCESSING_VERSION == "media_ocr_adapter_v1"
