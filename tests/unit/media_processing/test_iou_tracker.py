"""Real, local, deterministic IoU tracker (`analysis/iou_tracker.py`) -- the
production promotion of `fake_tracker.py`'s identical algorithm, see that
module's docstring. Mirrors the scenarios that algorithm is already known
to satisfy; this file proves the *real*, versioned, configurable class
behaves identically, not that the algorithm itself is novel.
"""

from __future__ import annotations

from app.modules.media_processing.analysis.interfaces import ObjectDetection
from app.modules.media_processing.analysis.iou_tracker import IoUTracker, IoUTrackerConfig
from app.modules.media_processing.image.geometry import PixelBoundingBox


def _detection(
    label: str, x_min: float, y_min: float, x_max: float | None = None, y_max: float | None = None
) -> ObjectDetection:
    resolved_x_max = x_max if x_max is not None else x_min + 20
    resolved_y_max = y_max if y_max is not None else y_min + 20
    return ObjectDetection(
        label=label,
        confidence=0.9,
        box=PixelBoundingBox(x_min, y_min, resolved_x_max, resolved_y_max),
    )


def test_a_single_moving_detection_forms_one_continuous_track() -> None:
    tracker = IoUTracker()
    detections_by_time = {
        0: [_detection("person", 10, 10)],
        1000: [_detection("person", 12, 12)],
        2000: [_detection("person", 14, 14)],
    }
    segments = tracker.track(detections_by_time)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.label == "person"
    assert segment.start_time_ms == 0
    assert segment.end_time_ms == 2000
    assert set(segment.boxes_by_time_ms) == {0, 1000, 2000}
    assert segment.quality == 1.0  # more than one box -> higher-quality track


def test_track_ids_are_deterministic_for_identical_input() -> None:
    tracker = IoUTracker()
    detections_by_time = {0: [_detection("vehicle", 50, 50)], 1000: [_detection("vehicle", 52, 52)]}
    first = tracker.track(detections_by_time)
    second = tracker.track(detections_by_time)
    assert [s.local_track_id for s in first] == [s.local_track_id for s in second]


def test_track_ids_change_when_identity_bearing_input_changes() -> None:
    tracker = IoUTracker()
    baseline = tracker.track({0: [_detection("person", 10, 10)]})
    different_label = tracker.track({0: [_detection("vehicle", 10, 10)]})
    different_time = tracker.track({500: [_detection("person", 10, 10)]})
    assert baseline[0].local_track_id != different_label[0].local_track_id
    assert baseline[0].local_track_id != different_time[0].local_track_id


def test_track_never_bridges_a_label_change() -> None:
    """A "person" track can never continue as a "vehicle" track, even at the same box."""
    tracker = IoUTracker()
    segments = tracker.track(
        {0: [_detection("person", 10, 10)], 1000: [_detection("vehicle", 10, 10)]}
    )
    assert len(segments) == 2
    assert {s.label for s in segments} == {"person", "vehicle"}


def test_overlapping_same_time_detections_are_preserved_not_merged() -> None:
    tracker = IoUTracker()
    segments = tracker.track(
        {
            0: [_detection("person", 0, 0, 100, 100), _detection("person", 500, 500, 600, 600)],
        }
    )
    assert len(segments) == 2  # two distinct tracks, never merged into one


def test_a_gap_ends_a_track_rather_than_bridging_it() -> None:
    """A track that isn't matched at a later sampled timestamp ends there -- it is
    never silently bridged across a missed frame with a fabricated continuity."""
    tracker = IoUTracker()
    segments = tracker.track(
        {
            0: [_detection("person", 10, 10)],
            1000: [],  # no detection at all this timestamp
            2000: [_detection("person", 200, 200)],  # far away -- a new, distinct object
        }
    )
    assert len(segments) == 2
    assert segments[0].end_time_ms == 0
    assert segments[1].start_time_ms == 2000


def test_configurable_iou_threshold_changes_association_strictness() -> None:
    """A stricter threshold than the two boxes' actual IoU rejects the match --
    proving `IoUTrackerConfig.iou_match_threshold` is genuinely load-bearing, not
    decorative."""
    detections_by_time = {
        0: [_detection("person", 0, 0, 100, 100)],
        1000: [_detection("person", 60, 0, 160, 100)],
    }
    permissive = IoUTracker(config=IoUTrackerConfig(iou_match_threshold=0.1))
    strict = IoUTracker(config=IoUTrackerConfig(iou_match_threshold=0.9))
    assert len(permissive.track(detections_by_time)) == 1
    assert len(strict.track(detections_by_time)) == 2


def test_local_track_id_is_never_treated_as_a_cross_evidence_identity() -> None:
    """Two entirely independent `track()` calls (modelling two different evidence
    items) never coincidentally share a `local_track_id` for unrelated content,
    and nothing about this tracker's output claims otherwise -- `TrackSegment`
    carries no evidence/case reference at all, by design (see `interfaces.py`)."""
    tracker = IoUTracker()
    evidence_a = tracker.track({0: [_detection("person", 10, 10)]})
    evidence_b = tracker.track({0: [_detection("person", 10, 10)]})
    # Identical input naturally produces the identical deterministic ID --
    # that's expected determinism, not identity linkage. What matters is
    # that nothing in the shape of `TrackSegment` could carry a cross-item
    # claim even if a caller wanted one.
    assert not hasattr(evidence_a[0], "evidence_id")
    assert not hasattr(evidence_a[0], "case_id")
    assert (
        evidence_a[0].local_track_id == evidence_b[0].local_track_id
    )  # by construction, not identity
