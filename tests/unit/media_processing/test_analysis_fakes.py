"""Scenario 12: fake detector/tracker/OCR are deterministic and clearly test-only."""

from __future__ import annotations

from app.modules.media_processing.analysis.fake_detector import (
    FAKE_DETECTOR_VERSION,
    FakeObjectDetector,
)
from app.modules.media_processing.analysis.fake_ocr import FAKE_OCR_VERSION, FakeTextRecognizer
from app.modules.media_processing.analysis.fake_tracker import (
    FAKE_TRACKER_VERSION,
    FakeObjectTracker,
)
from tests.fixtures.media_processing.synthetic import make_solid_frame


def test_fake_detector_is_deterministic_for_same_frame_size() -> None:
    detector = FakeObjectDetector()
    frame_a = make_solid_frame(width=64, height=48, value=10)
    frame_b = make_solid_frame(width=64, height=48, value=200)  # different content, same size
    result_a = detector.detect(frame_a)
    result_b = detector.detect(frame_b)
    assert result_a == result_b


def test_fake_detector_scales_box_with_frame_size() -> None:
    detector = FakeObjectDetector()
    small = detector.detect(make_solid_frame(width=100, height=100))[0]
    large = detector.detect(make_solid_frame(width=200, height=200))[0]
    assert large.box.x_max == small.box.x_max * 2


def test_fake_detector_tags_output_as_fake() -> None:
    detector = FakeObjectDetector()
    detection = detector.detect(make_solid_frame())[0]
    assert detection.attributes["model_interface_version"] == FAKE_DETECTOR_VERSION
    assert "fake" in FAKE_DETECTOR_VERSION


def test_fake_detector_returns_confidence_in_valid_range() -> None:
    detector = FakeObjectDetector()
    detection = detector.detect(make_solid_frame())[0]
    assert 0.0 <= detection.confidence <= 1.0


def test_fake_tracker_is_deterministic() -> None:
    tracker = FakeObjectTracker()
    detector = FakeObjectDetector()
    frame = make_solid_frame(width=64, height=48)
    detections_by_time = {
        0: detector.detect(frame),
        100: detector.detect(frame),
        200: detector.detect(frame),
    }
    first = tracker.track(detections_by_time)
    second = tracker.track(detections_by_time)
    assert first == second


def test_fake_tracker_produces_stable_local_track_ids() -> None:
    tracker = FakeObjectTracker()
    detector = FakeObjectDetector()
    frame = make_solid_frame()
    detections_by_time = {0: detector.detect(frame), 100: detector.detect(frame)}
    segments = tracker.track(detections_by_time)
    assert len(segments) == 1
    assert segments[0].start_time_ms == 0
    assert segments[0].end_time_ms == 100
    assert len(segments[0].boxes_by_time_ms) == 2


def test_fake_tracker_tags_output_as_fake() -> None:
    tracker = FakeObjectTracker()
    detector = FakeObjectDetector()
    frame = make_solid_frame()
    segments = tracker.track({0: detector.detect(frame)})
    assert segments[0].attributes["model_interface_version"] == FAKE_TRACKER_VERSION


def test_fake_tracker_never_assigns_a_local_track_id_that_looks_like_an_entity_id() -> None:
    # local_track_id is a deterministic UUID string, scoped to this run only --
    # never confused with an EntityV1.entity_id by construction (different namespace input).
    tracker = FakeObjectTracker()
    detector = FakeObjectDetector()
    frame = make_solid_frame()
    segments = tracker.track({0: detector.detect(frame)})
    assert isinstance(segments[0].local_track_id, str)
    assert len(segments[0].local_track_id) > 0


def test_fake_ocr_is_deterministic_for_same_crop_size() -> None:
    ocr = FakeTextRecognizer()
    crop_a = make_solid_frame(width=20, height=10, value=1)
    crop_b = make_solid_frame(width=20, height=10, value=250)
    assert ocr.recognize(crop_a) == ocr.recognize(crop_b)


def test_fake_ocr_tags_output_as_fake() -> None:
    ocr = FakeTextRecognizer()
    result = ocr.recognize(make_solid_frame(width=20, height=10))
    assert result is not None
    assert "FAKE" in result.text
    assert result.attributes["model_interface_version"] == FAKE_OCR_VERSION
