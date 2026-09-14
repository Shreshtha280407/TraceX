"""Real, local, deterministic tracker: greedy class-aware IoU association.

No ML model, no external tracking library (ByteTrack/DeepSORT/etc.) -- IoU
(Intersection-over-Union) association across consecutive sampled timestamps
is a standard, well-documented, non-learned tracking-by-detection policy
(the same core association step SORT/ByteTrack build on top of, minus their
Kalman-filter motion prediction, which this module's bounded, deterministic,
evidence-local scope does not need). Given the same `detections_by_time_ms`
input, this tracker always produces the same `TrackSegment`s -- no
randomness, no learned weights, nothing to bootstrap or verify a checksum
for.

**Policy** (see `IoUTrackerConfig.iou_match_threshold`): at each sampled
timestamp (processed in ascending time order, regardless of input mapping
order), every currently-active track is greedily matched to the
same-labelled detection with the highest IoU against its last known box,
provided that IoU is at least `iou_match_threshold`; unmatched active
tracks are closed (a gap ends a track, it is never "bridged" across a
missed frame); every detection left unmatched starts a new track. A track
is never matched across a change in `label` (a "person" track can never
continue as a "vehicle" track), and a track's `local_track_id` is
deterministic (`app.core.ids.deterministic_uuid`) but valid only within one
`detect()`/`track()` call for one evidence item -- never a cross-evidence or
cross-case identity, and never a claim that the same real-world object
appears in two different tracks/evidence items (see `CLAUDE.md`: no
automatic identity merge).

This is the same algorithm `analysis/fake_tracker.py`'s `FakeObjectTracker`
already implements (kept there, unchanged, as the fixed, config-free
deterministic stand-in existing CI tests depend on) -- this module exists
so the *real*, production analysis pipeline (`worker.py`'s
`_build_analysis_components`) uses a named, versioned, independently
configurable tracker rather than importing a class whose own docstring
says "test-only".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from pydantic import JsonValue

from app.core.ids import deterministic_uuid
from app.modules.media_processing.analysis.interfaces import ObjectDetection, TrackSegment
from app.modules.media_processing.image.geometry import PixelBoundingBox

IOU_TRACKER_VERSION = "iou_tracker_v1"

#: Default match threshold: a candidate detection must overlap a track's
#: last known box by at least this fraction (intersection / union) to
#: extend it. Deliberately permissive (not e.g. 0.3-0.5, common in
#: ML-tracker literature tuned for high-frame-rate video) because this
#: module's frame sampling is comparatively sparse (default: one frame per
#: second, see `video/sampling.py`) -- a real moving subject can shift
#: substantially between sampled frames, so a stricter threshold would
#: fragment genuine tracks into many short ones.
DEFAULT_IOU_MATCH_THRESHOLD = 0.1


@dataclass(frozen=True)
class IoUTrackerConfig:
    """Typed, explicit tracker configuration."""

    iou_match_threshold: float = DEFAULT_IOU_MATCH_THRESHOLD


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
    lifecycle_conditions: set[str] = field(default_factory=lambda: {"source_local"})


@dataclass(frozen=True)
class IoUTracker:
    """Real, local, deterministic greedy class-aware IoU tracker. See module docstring."""

    config: IoUTrackerConfig = field(default_factory=IoUTrackerConfig)

    def track(
        self, detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
    ) -> list[TrackSegment]:
        active: list[_ActiveTrack] = []
        finished: list[_ActiveTrack] = []
        for time_ms in sorted(detections_by_time_ms):
            unmatched = list(detections_by_time_ms[time_ms])
            still_active: list[_ActiveTrack] = []
            for existing_track in active:
                best_index, best_iou = None, self.config.iou_match_threshold
                for index, detection in enumerate(unmatched):
                    if detection.label != existing_track.label:
                        continue
                    score = _iou(existing_track.last_box, detection.box)
                    if score >= best_iou:
                        best_index, best_iou = index, score
                if best_index is None:
                    existing_track.lifecycle_conditions.add("ended_unmatched")
                    finished.append(existing_track)
                    continue
                detection = unmatched.pop(best_index)
                if (
                    sum(
                        1
                        for candidate in detections_by_time_ms[time_ms]
                        if candidate.label == existing_track.label
                        and _iou(existing_track.last_box, candidate.box)
                        >= self.config.iou_match_threshold
                    )
                    > 1
                ):
                    existing_track.lifecycle_conditions.add("split_ambiguous")
                existing_track.last_time_ms = time_ms
                existing_track.last_box = detection.box
                existing_track.boxes_by_time_ms[time_ms] = detection.box
                still_active.append(existing_track)

            for detection in unmatched:
                conditions = {"source_local"}
                if any(track.label == detection.label for track in finished):
                    conditions.add("reappearance_unlinked")
                track_id = str(
                    deterministic_uuid(
                        IOU_TRACKER_VERSION,
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
                        lifecycle_conditions=conditions,
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
                attributes={
                    "model_interface_version": IOU_TRACKER_VERSION,
                    "track_lifecycle_conditions": cast(
                        list[JsonValue], sorted(track.lifecycle_conditions)
                    ),
                },
            )
            for track in finished
        ]


__all__ = ["DEFAULT_IOU_MATCH_THRESHOLD", "IOU_TRACKER_VERSION", "IoUTracker", "IoUTrackerConfig"]
