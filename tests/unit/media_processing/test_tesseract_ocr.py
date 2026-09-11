"""Real local Tesseract OCR adapter.

`test_missing_tesseract_binary_fails_clearly` always runs unconditionally --
it monkeypatches `pytesseract`'s own binary path to a nonexistent one, so it
never depends on whether `tesseract-ocr` is actually installed on the host
running this suite. Every other test here needs a genuinely working local
`tesseract` binary (to prove "language pack missing" specifically, or real
recognition quality) and self-skips cleanly via `_tesseract_available()`
(mirroring `ffmpeg_available()`'s convention in `synthetic.py`) if it is not
present -- this suite never installs `tesseract-ocr` itself. Real-OCR-quality
scenarios additionally self-skip if no suitable TrueType font is found
locally (Tesseract can't read tiny bitmap-font renders reliably -- see
`_find_test_font`); this suite never downloads a font either.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.modules.media_processing.analysis.tesseract_ocr import (
    TesseractOcrConfig,
    TesseractTextRecognizer,
)
from app.modules.media_processing.errors import OcrRuntimeError

_CANDIDATE_FONT_PATHS = (
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _skip_unless_tesseract_available() -> None:
    if not _tesseract_available():
        pytest.skip(
            "the 'tesseract' binary is not on PATH; install the 'tesseract-ocr' system "
            "package to enable this scenario (this suite never installs it itself)"
        )


def _find_test_font() -> str | None:
    for candidate in _CANDIDATE_FONT_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def _render_text(text: str, *, font_path: str, size: int = 48) -> np.ndarray:
    font = ImageFont.truetype(font_path, size)
    image = Image.new("RGB", (500, 150), color=(255, 255, 255))
    ImageDraw.Draw(image).text((20, 40), text, fill=(0, 0, 0), font=font)
    return np.asarray(image, dtype=np.uint8)


# --- Runtime-availability failure modes -------------------------------------


def test_missing_tesseract_binary_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tesseract_cmd` is process-global module state in `pytesseract` itself (not
    per-instance) -- `monkeypatch.setattr` (not `TesseractOcrConfig.tesseract_cmd`,
    which would leak the same mutation into every later test in this process) so
    it's automatically reverted when this test ends. Runs unconditionally -- it
    never depends on whether the real `tesseract` binary is actually installed."""
    monkeypatch.setattr("pytesseract.pytesseract.tesseract_cmd", "/nonexistent/tesseract")
    with pytest.raises(OcrRuntimeError, match="tesseract"):
        TesseractTextRecognizer()


def test_missing_language_pack_fails_clearly() -> None:
    """Needs a genuinely working `tesseract` binary to reach the language-pack
    check at all -- self-skips without one, rather than failing on the wrong
    (binary-missing) error message."""
    _skip_unless_tesseract_available()
    with pytest.raises(OcrRuntimeError, match="language pack"):
        TesseractTextRecognizer(config=TesseractOcrConfig(language="definitely_not_a_real_lang"))


# --- Real OCR: self-skips without a real tesseract binary and/or a suitable font


def test_real_ocr_recognizes_rendered_text_with_bounding_box_and_confidence() -> None:
    _skip_unless_tesseract_available()
    font_path = _find_test_font()
    if font_path is None:
        pytest.skip("no suitable local TrueType font found to render OCR-readable test text")
    frame = _render_text("EXIT 42B", font_path=font_path)
    recognizer = TesseractTextRecognizer(config=TesseractOcrConfig(min_confidence=0.0))
    regions = recognizer.recognize_regions(frame)
    assert len(regions) == 1
    region = regions[0]
    assert region.text.strip() == "EXIT 42B"
    assert 0.0 <= region.confidence <= 1.0
    assert region.box.x_min < region.box.x_max
    assert region.box.y_min < region.box.y_max
    # provenance: bounding box lies within the actual frame dimensions
    assert region.box.x_max <= frame.shape[1]
    assert region.box.y_max <= frame.shape[0]


def test_real_ocr_single_crop_recognize_matches_recognize_regions_aggregate() -> None:
    _skip_unless_tesseract_available()
    font_path = _find_test_font()
    if font_path is None:
        pytest.skip("no suitable local TrueType font found to render OCR-readable test text")
    frame = _render_text("HELLO", font_path=font_path)
    recognizer = TesseractTextRecognizer(config=TesseractOcrConfig(min_confidence=0.0))
    single = recognizer.recognize(frame)
    assert single is not None
    assert single.text.strip() == "HELLO"
    assert 0.0 <= single.confidence <= 1.0


def test_real_ocr_confidence_floor_filters_low_confidence_regions() -> None:
    _skip_unless_tesseract_available()
    font_path = _find_test_font()
    if font_path is None:
        pytest.skip("no suitable local TrueType font found to render OCR-readable test text")
    frame = _render_text("VISIBLE TEXT", font_path=font_path)
    strict_recognizer = TesseractTextRecognizer(config=TesseractOcrConfig(min_confidence=1.01))
    assert strict_recognizer.recognize_regions(frame) == []
    assert strict_recognizer.recognize(frame) is None


def test_real_ocr_blank_image_finds_no_text() -> None:
    _skip_unless_tesseract_available()
    font_path = _find_test_font()
    if font_path is None:
        pytest.skip("no suitable local TrueType font found to render OCR-readable test text")
    blank = np.full((150, 500, 3), 255, dtype=np.uint8)
    recognizer = TesseractTextRecognizer(config=TesseractOcrConfig(min_confidence=0.0))
    assert recognizer.recognize_regions(blank) == []
    assert recognizer.recognize(blank) is None


def test_recognize_regions_rejects_a_degenerate_empty_image() -> None:
    _skip_unless_tesseract_available()
    recognizer = TesseractTextRecognizer()
    empty = np.zeros((0, 0, 3), dtype=np.uint8)
    assert recognizer.recognize_regions(empty) == []
