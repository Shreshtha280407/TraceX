"""A deterministic, test-only text recognizer.

No real OCR engine is implemented in this phase. This stub's output
depends only on crop dimensions, never on pixel content, and its text is
unmistakably fake-labeled -- it must never be confused with real OCR
output. Must always be passed to the worker explicitly -- it is never a
silent default (see `worker.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.media_processing.analysis.interfaces import RecognizedText
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import Frame

FAKE_OCR_VERSION = "fake_ocr_v1"


@dataclass(frozen=True)
class FakeTextRecognizer:
    """Deterministic, test-only OCR stub."""

    confidence: float = 0.5

    def recognize(self, crop: Frame) -> RecognizedText | None:
        height, width = int(crop.shape[0]), int(crop.shape[1])
        if height <= 0 or width <= 0:
            return None
        return RecognizedText(
            text=f"FAKE_OCR_TEXT_{width}x{height}",
            confidence=self.confidence,
            box=PixelBoundingBox(x_min=0, y_min=0, x_max=width, y_max=height),
            attributes={"model_interface_version": FAKE_OCR_VERSION},
        )
