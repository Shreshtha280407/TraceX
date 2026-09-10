"""A deterministic, test-only object tracker.

Greedy same-label IoU matching across sampled timestamps, processed in
ascending time order regardless of input mapping order -- the same
`detections_by_time_ms` input always yields the same `TrackSegment`s.
`local_track_id` is a `deterministic_uuid` string; it is valid only inside
one evidence item/processing run and is never a person or vehicle identity
(see `CLAUDE.md`: no automatic identity merge). Must always be passed to
the worker explicitly -- it is never a silent default (see `worker.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.core.ids import deterministic_uuid
from app.modules.media_processing.analysis.interfaces import ObjectDetection, TrackSegment
from app.modules.media_processing.image.geometry import PixelBoundingBox

FAKE_TRACKER_VERSION = "fake_tracker_v1"
_IOU_MATCH_THRESHOLD = 0.1


def _iou(a: PixelBoundingBox, b: PixelBoundingBox) -> float:
    x_min, y_min = max(a.x_min, b.x_min), max(a.y_min, b.y_min)
    x_max, y_max = min(a.x_max, b.x_max), min(a.y_max, b.y_max)
    if x_max <= x_min or y_max <= y_min:
        return 0.0
    intersection = (x_max - x_min) * (y_max - y_min)
    area_a = (a.x_max - a.x_min) * (a.y_max - a.y_min)
    area_b = (b.x_max - b.x_min) * (b.y_max - b.y_min)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


@dataclass
class _ActiveTrack:
    local_track_id: str
    label: str
    start_time_ms: int
    last_time_ms: int
    last_box: PixelBoundingBox
    boxes_by_time_ms: dict[int, PixelBoundingBox] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeObjectTracker:
    """Deterministic, test-only greedy same-label IoU tracker."""

    def track(
        self, detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
    ) -> list[TrackSegment]:
        active: list[_ActiveTrack] = []
        finished: list[_ActiveTrack] = []
        for time_ms in sorted(detections_by_time_ms):
            unmatched = list(detections_by_time_ms[time_ms])
            still_active: list[_ActiveTrack] = []
            for track in active:
                best_index, best_iou = None, _IOU_MATCH_THRESHOLD
                for index, detection in enumerate(unmatched):
                    if detection.label != track.label:
                        continue
                    score = _iou(track.last_box, detection.box)
                    if score >= best_iou:
                        best_index, best_iou = index, score
                if best_index is None:
                    finished.append(track)
                    continue
                detection = unmatched.pop(best_index)
                track.last_time_ms = time_ms
                track.last_box = detection.box
                track.boxes_by_time_ms[time_ms] = detection.box
                still_active.append(track)

            for detection in unmatched:
                track_id = str(
                    deterministic_uuid(
                        FAKE_TRACKER_VERSION,
                        detection.label,
                        str(time_ms),
                        str(len(still_active)),
                    )
                )
                still_active.append(
                    _ActiveTrack(
                        local_track_id=track_id,
                        label=detection.label,
                        start_time_ms=time_ms,
                        last_time_ms=time_ms,
                        last_box=detection.box,
                        boxes_by_time_ms={time_ms: detection.box},
                    )
                )
            active = still_active

        finished.extend(active)
        return [
            TrackSegment(
                local_track_id=track.local_track_id,
                label=track.label,
                start_time_ms=track.start_time_ms,
                end_time_ms=track.last_time_ms,
                boxes_by_time_ms=dict(track.boxes_by_time_ms),
                quality=1.0 if len(track.boxes_by_time_ms) > 1 else 0.5,
                attributes={"model_interface_version": FAKE_TRACKER_VERSION},
            )
            for track in finished
        ]
