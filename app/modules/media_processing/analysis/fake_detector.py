"""A deterministic, test-only object detector.

Never a real model: always returns the same detection for a given frame
size, tagged with `FAKE_DETECTOR_VERSION` so its output is never confused
with a real model's confidence (see `provenance.py`'s confidence policy).
Must always be passed to the worker explicitly -- it is never a silent
default (see `worker.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.media_processing.analysis.interfaces import ObjectDetection
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import Frame

FAKE_DETECTOR_VERSION = "fake_detector_v1"


@dataclass(frozen=True)
class FakeObjectDetector:
    """Always emits one fixed-confidence, centrally-placed detection box.

    Deterministic: depends only on frame dimensions, never on pixel
    content, so the same-sized frame always yields the identical detection.
    """

    label: str = "person"
    confidence: float = 0.99

    def detect(self, frame: Frame) -> list[ObjectDetection]:
        height, width = int(frame.shape[0]), int(frame.shape[1])
        if height <= 0 or width <= 0:
            return []
        box = PixelBoundingBox(
            x_min=width * 0.3, y_min=height * 0.3, x_max=width * 0.7, y_max=height * 0.7
        )
        return [
            ObjectDetection(
                label=self.label,
                confidence=self.confidence,
                box=box,
                attributes={"model_interface_version": FAKE_DETECTOR_VERSION},
            )
        ]
