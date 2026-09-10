"""Typed, pluggable detection/tracking/OCR interfaces.

These are protocols only -- no real detector, tracker, or OCR engine is
implemented in this phase (see `CLAUDE.md`). A later phase may adapt a
local YOLO/ByteTrack/PaddleOCR model behind these same interfaces without
this module's worker/provenance code changing. Only deterministic fake
implementations (`analysis/fake_*.py`) exist today, and they must always be
passed in explicitly -- never selected as a silent default (see
`worker.py`).

None of these types carry face identity, person names, demographic
inference, or criminality labels -- see `SUPPORTED_DETECTION_LABELS` and
`docs/architecture/media-observation-taxonomy-v1.md`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import Frame

#: Recommended initial detection labels. Enforced only by convention in the
#: fake detector (`fake_detector.py`) -- a future real detector's own
#: label set is that adapter's responsibility to keep safe.
SUPPORTED_DETECTION_LABELS = frozenset(
    {
        "person",
        "vehicle",
        "motorcycle",
        "bicycle",
        "bus",
        "truck",
        "number_plate_region",
        "text_region",
    }
)


@dataclass(frozen=True)
class ObjectDetection:
    """One detection in one frame/image. Anonymous -- not an identity claim."""

    label: str
    confidence: float
    box: PixelBoundingBox
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


@runtime_checkable
class ObjectDetector(Protocol):
    """Detects objects in one decoded frame/image."""

    def detect(self, frame: Frame) -> list[ObjectDetection]: ...


@dataclass(frozen=True)
class TrackSegment:
    """An anonymous local association of detections across sampled frames.

    `local_track_id` is valid only inside one evidence item and one
    processing run -- it is never a person or vehicle identity, and must
    never be treated as one downstream (see `CLAUDE.md`: no automatic
    identity merge).
    """

    local_track_id: str
    label: str
    start_time_ms: int
    end_time_ms: int
    boxes_by_time_ms: Mapping[int, PixelBoundingBox]
    quality: float
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


@runtime_checkable
class ObjectTracker(Protocol):
    """Associates anonymous detections across sequential sampled frames."""

    def track(
        self, detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
    ) -> list[TrackSegment]: ...


@dataclass(frozen=True)
class RecognizedText:
    """Text recognized in one explicitly-selected image crop."""

    text: str
    confidence: float
    box: PixelBoundingBox
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


@runtime_checkable
class TextRecognizer(Protocol):
    """Recognizes text in one explicitly-selected crop. No real OCR engine
    is implemented in this phase -- see `fake_ocr.py`."""

    def recognize(self, crop: Frame) -> RecognizedText | None: ...
