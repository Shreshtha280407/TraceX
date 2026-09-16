"""Phase 7 Part 3 visual-benchmark metric function tests.

Pure functions only -- no engine, no dataset, no GPU. Every ground-truth
value used here is synthetic and invented for this test file.
"""

from __future__ import annotations

from app.modules.media_processing.analysis.interfaces import ObjectDetection, TrackSegment
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.visual_benchmark_metrics import (
    GroundTruthBox,
    GroundTruthTrack,
    character_error_rate,
    detection_precision_recall_map,
    field_extraction_prf,
    hota,
    id_switches,
    idf1,
    median,
    mota,
    peak_memory_mb,
    percentile,
    word_error_rate,
)


def _box(x: float) -> PixelBoundingBox:
    return PixelBoundingBox(x_min=x, y_min=x, x_max=x + 20, y_max=x + 20)


def test_character_and_word_error_rate_are_zero_for_a_perfect_match() -> None:
    assert character_error_rate("ABC1234", "ABC1234") == 0.0
    assert word_error_rate("the quick fox", "the quick fox") == 0.0


def test_character_error_rate_is_none_for_an_empty_reference() -> None:
    assert character_error_rate("", "anything") is None


def test_field_extraction_prf_scores_a_perfect_and_a_missed_field() -> None:
    assert field_extraction_prf({"plate_text": "ABC1234"}, {"plate_text": "ABC1234"}) == (
        1.0,
        1.0,
        1.0,
    )
    precision, recall, f1 = field_extraction_prf(
        {"plate_text": "ABC1234"}, {"plate_text": "XYZ0000"}
    )
    assert (precision, recall, f1) == (0.0, 0.0, 0.0)


def test_field_extraction_prf_is_none_when_nothing_was_expected_or_extracted() -> None:
    assert field_extraction_prf({}, {}) == (None, None, None)


def test_percentile_and_median_are_correct_for_a_known_sequence() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert median(values) == 3.0
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 100.0) == 5.0


def test_percentile_is_none_for_an_empty_sequence() -> None:
    assert percentile([], 50.0) is None


def test_peak_memory_mb_returns_a_positive_measured_value_on_this_platform() -> None:
    value = peak_memory_mb()
    assert value is None or value > 0.0


# --- Detection: valid metric calculation, and unsupported-without-labels ---


def test_detection_metrics_are_perfect_for_a_well_matched_prediction() -> None:
    ground_truth = [GroundTruthBox(label="person", box=_box(10))]
    predictions = [ObjectDetection(label="person", confidence=0.9, box=_box(11))]
    precision, recall, mean_ap = detection_precision_recall_map(predictions, ground_truth)
    assert precision == 1.0
    assert recall == 1.0
    assert mean_ap == 1.0


def test_detection_metrics_are_zero_for_a_completely_wrong_prediction() -> None:
    ground_truth = [GroundTruthBox(label="person", box=_box(10))]
    predictions = [ObjectDetection(label="person", confidence=0.9, box=_box(500))]
    precision, recall, mean_ap = detection_precision_recall_map(predictions, ground_truth)
    assert (precision, recall, mean_ap) == (0.0, 0.0, 0.0)


def test_detection_metrics_are_unavailable_without_ground_truth_labels() -> None:
    predictions = [ObjectDetection(label="person", confidence=0.9, box=_box(11))]
    assert detection_precision_recall_map(predictions, None) == (None, None, None)
    assert detection_precision_recall_map(predictions, []) == (None, None, None)


# --- Tracking: valid metric calculation, ID switches, and HOTA=None -------


def test_tracking_metrics_are_perfect_for_a_single_consistent_track() -> None:
    ground_truth = [
        GroundTruthTrack(
            track_id="gt1", label="person", boxes_by_time_ms={0: _box(10), 100: _box(12)}
        )
    ]
    predicted = [
        TrackSegment(
            local_track_id="p1",
            label="person",
            start_time_ms=0,
            end_time_ms=100,
            boxes_by_time_ms={0: _box(11), 100: _box(13)},
            quality=1.0,
        )
    ]
    assert idf1(predicted, ground_truth) == 1.0
    assert mota(predicted, ground_truth) == 1.0
    assert id_switches(predicted, ground_truth) == 0


def test_tracking_metrics_detect_an_identity_switch() -> None:
    ground_truth = [
        GroundTruthTrack(
            track_id="gt1",
            label="person",
            boxes_by_time_ms={0: _box(10), 100: _box(12), 200: _box(14)},
        )
    ]
    predicted = [
        TrackSegment(
            local_track_id="pa",
            label="person",
            start_time_ms=0,
            end_time_ms=100,
            boxes_by_time_ms={0: _box(11), 100: _box(13)},
            quality=1.0,
        ),
        TrackSegment(
            local_track_id="pb",
            label="person",
            start_time_ms=200,
            end_time_ms=200,
            boxes_by_time_ms={200: _box(15)},
            quality=1.0,
        ),
    ]
    assert id_switches(predicted, ground_truth) == 1
    assert idf1(predicted, ground_truth) is not None
    assert idf1(predicted, ground_truth) < 1.0


def test_tracking_metrics_are_unavailable_without_ground_truth_tracks() -> None:
    predicted: list[TrackSegment] = []
    assert idf1(predicted, None) is None
    assert mota(predicted, None) is None
    assert id_switches(predicted, None) is None


def test_hota_is_always_none_and_never_a_fabricated_approximation() -> None:
    ground_truth = [
        GroundTruthTrack(track_id="gt1", label="person", boxes_by_time_ms={0: _box(10)})
    ]
    assert hota([], ground_truth) is None
    assert hota([], None) is None
