"""Regression coverage for the deterministic pre-OCR image binarization step.

Added after a confirmed macOS Gate B defect (Tesseract 5.5.0): the same
rendered page produced a digit-insertion misread (`25000` -> `250000`) and
dropped the FIR-number line entirely (no `fir_reference` at all) on that
platform, while passing on the CI/dev environment's Tesseract version.
Tesseract's own internal adaptive thresholding is exactly what varies
between versions/platforms; these tests protect the new,
version-independent alternative (`_prepare_image_for_ocr`) directly,
without depending on any specific Tesseract behavior.
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


def test_ocr_config_hash_reflects_binarization_settings() -> None:
    """Provenance: a changed preprocessing config must change the recorded hash."""
    base = ocr_config_hash(OcrConfig())
    no_binarize = ocr_config_hash(OcrConfig(binarize=False))
    different_threshold = ocr_config_hash(OcrConfig(binarization_threshold=200))
    assert base != no_binarize
    assert base != different_threshold
    assert no_binarize != different_threshold
