"""Regression coverage for Gate B's macOS OCR configuration decisions.

`OcrConfig.binarize` was briefly `True` by default (commit f84aa4d) on the
theory that a fixed threshold would be more cross-platform-portable than
Tesseract's own internal adaptive thresholding. Real Gate B measurements on
two macOS Tesseract builds (5.5.0, 5.5.3) disproved that: the fixed
threshold destroyed valid characters Tesseract's own adaptive thresholding
read correctly unassisted on that platform. It is now `False` by default.

`OcrConfig.page_segmentation_mode` defaults to `6`, not `3`, per the same
Gate B matrix (real macOS Tesseract 5.5.3): PSM 6 was the configuration
that actually read the FIR-label line correctly; other tested PSM values
were not better on that machine. See `docs/architecture/
document-structured-processing.md`'s "Gate B macOS OCR configuration"
section for the full matrix this decision is based on.

These tests protect `_prepare_image_for_ocr` (still available as an
explicit opt-in) and both defaults directly, without depending on any
specific Tesseract behavior, and guard against either default silently
regressing.
"""

from __future__ import annotations

from PIL import Image

from app.modules.structured_processing.document.ocr import (
    OcrConfig,
    _prepare_image_for_ocr,
    ocr_config_hash,
)


def _gray_gradient_image() -> Image.Image:
    """A 256x4 RGB image with every 8-bit gray value present in each row."""
    image = Image.new("RGB", (256, 4))
    pixels = image.load()
    for x in range(256):
        for y in range(4):
            pixels[x, y] = (x, x, x)
    return image


def test_binarize_true_produces_only_two_pixel_values() -> None:
    image = _gray_gradient_image()
    prepared = _prepare_image_for_ocr(image, OcrConfig(binarize=True))
    assert set(prepared.get_flattened_data()) <= {0, 255}
    # A full 0-255 gradient must actually split into two, non-degenerate
    # groups -- proves the threshold is genuinely applied, not a no-op
    # that happens to already be two-valued.
    assert 0 in prepared.get_flattened_data()
    assert 255 in prepared.get_flattened_data()


def test_binarize_false_is_a_true_passthrough() -> None:
    image = _gray_gradient_image()
    prepared = _prepare_image_for_ocr(image, OcrConfig(binarize=False))
    assert prepared is image


def test_binarization_threshold_is_configurable_and_deterministic() -> None:
    image = _gray_gradient_image()
    low = _prepare_image_for_ocr(image, OcrConfig(binarize=True, binarization_threshold=10))
    high = _prepare_image_for_ocr(image, OcrConfig(binarize=True, binarization_threshold=245))
    # A lower cut point classifies more pixels as white than a higher one.
    assert sum(1 for p in low.get_flattened_data() if p == 255) > sum(
        1 for p in high.get_flattened_data() if p == 255
    )
    # Same config, same input -> identical output (no hidden randomness/state).
    again = _prepare_image_for_ocr(image, OcrConfig(binarize=True, binarization_threshold=10))
    assert list(low.get_flattened_data()) == list(again.get_flattened_data())


def test_preprocessing_does_not_change_image_dimensions() -> None:
    image = _gray_gradient_image()
    prepared = _prepare_image_for_ocr(image, OcrConfig(binarize=True))
    assert prepared.size == image.size


def test_binarize_defaults_to_off() -> None:
    """Guards against the default silently flipping back on (see module docstring)."""
    assert OcrConfig().binarize is False


def test_page_segmentation_mode_defaults_to_6() -> None:
    """Guards the Gate B macOS matrix decision (PSM 6) against silent regression.

    See `docs/architecture/document-structured-processing.md`'s "Gate B
    macOS OCR configuration" section for the real measured matrix.
    """
    assert OcrConfig().page_segmentation_mode == 6


def test_ocr_config_hash_reflects_binarization_settings() -> None:
    """Provenance: a changed preprocessing config must change the recorded hash."""
    default = ocr_config_hash(OcrConfig())
    opted_in = ocr_config_hash(OcrConfig(binarize=True))
    different_threshold = ocr_config_hash(OcrConfig(binarize=True, binarization_threshold=200))
    assert default != opted_in
    assert default != different_threshold
    assert opted_in != different_threshold


def test_ocr_config_hash_reflects_page_segmentation_mode() -> None:
    """Provenance: a changed PSM must also change the recorded hash."""
    default = ocr_config_hash(OcrConfig())
    other_psm = ocr_config_hash(OcrConfig(page_segmentation_mode=3))
    assert default != other_psm
