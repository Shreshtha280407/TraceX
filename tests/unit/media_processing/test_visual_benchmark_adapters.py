"""Phase 7 Part 3 visual-benchmark adapter tests.

Every test here uses a `Fake*Engine` and small synthetic in-memory frames
(`tests/fixtures/media_processing/synthetic.py::make_solid_frame`) -- no
real YOLO/ByteTrack/PaddleOCR model, GPU, or dataset is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from app.contracts.common import SourceLocator
from app.modules.evaluation.models import BenchmarkRunStatus, SplitId
from app.modules.media_processing.analysis.interfaces import (
    ObjectDetection,
    RecognizedText,
    TrackSegment,
)
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.visual_benchmark_adapters import (
    TRACKER_FAKE_EXEC_FAIL_LABEL,
    TRACKER_FAKE_UNREADABLE_LABEL,
    VISUAL_TEXT_FIELD_KEY,
    DetectionBenchmarkSample,
    DetectorEngine,
    FakeDetectorEngine,
    FakeTrackerEngine,
    FakeVisualTextEngine,
    TrackerEngine,
    TrackingBenchmarkSample,
    VisualTextBenchmarkSample,
    VisualTextEngine,
    build_observation_draft_for_detection,
    build_observation_draft_for_track_segment,
    build_observation_draft_for_visual_text,
    observation_for_draft,
    run_detection_benchmark,
    run_tracking_benchmark,
    run_visual_text_benchmark,
)
from app.modules.media_processing.visual_benchmark_metrics import GroundTruthBox, GroundTruthTrack
from tests.fixtures.media_processing.synthetic import make_solid_frame

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_BOX = PixelBoundingBox(x_min=10, y_min=10, x_max=50, y_max=50)


def _detection_sample(
    sample_id: str = "s1",
    *,
    frame_shape: tuple[int, int, int] | None = None,
    ground_truth: tuple[GroundTruthBox, ...] | None = None,
) -> DetectionBenchmarkSample:
    frame = make_solid_frame() if frame_shape is None else np.zeros(frame_shape, dtype=np.uint8)
    return DetectionBenchmarkSample(sample_id=sample_id, frame=frame, ground_truth=ground_truth)


def test_fake_detector_engine_produces_a_valid_succeeded_run() -> None:
    detection = ObjectDetection(label="person", confidence=0.9, box=_BOX)
    engine = FakeDetectorEngine(fixed_detections=(detection,))
    ground_truth = (GroundTruthBox(label="person", box=_BOX),)
    sample = _detection_sample(ground_truth=ground_truth)

    run = run_detection_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="yolo11n",
        dataset_id="virat_ground",
        split_id=SplitId.DEVELOPMENT,
        inference_config={"candidate_id": "yolo11n"},
        inference_config_hash="a" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["precision"] == 1.0
    assert run.metrics["recall"] == 1.0
    assert run.artifact_sha256 == "0" * 64
    assert run.hardware_profile == "fake-cpu"


def test_detection_benchmark_rejects_an_empty_sample_list() -> None:
    engine = FakeDetectorEngine()
    with pytest.raises(ValueError, match="at least one sample"):
        run_detection_benchmark(
            engine=engine,
            samples=[],
            candidate_id="yolo11n",
            dataset_id="virat_ground",
            split_id=SplitId.DEVELOPMENT,
            inference_config={},
            inference_config_hash="a" * 64,
        )


def test_detection_benchmark_categorizes_a_per_sample_failure_safely() -> None:
    engine = FakeDetectorEngine()
    sample = _detection_sample(frame_shape=FakeDetectorEngine.UNREADABLE_SHAPE)
    run = run_detection_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="yolo11n",
        dataset_id="virat_ground",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="a" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_failure_count_unreadable"] == 1
    assert run.artifact_sha256 is None


def test_detection_benchmark_fails_safely_when_every_sample_fails() -> None:
    engine = FakeDetectorEngine()
    samples = [
        _detection_sample("bad1", frame_shape=FakeDetectorEngine.UNREADABLE_SHAPE),
        _detection_sample("bad2", frame_shape=FakeDetectorEngine.EXECUTION_FAILURE_SHAPE),
    ]
    run = run_detection_benchmark(
        engine=engine,
        samples=samples,
        candidate_id="yolo11n",
        dataset_id="virat_ground",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="a" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_success_count"] == 0


def test_fake_tracker_engine_produces_a_valid_succeeded_run() -> None:
    track = TrackSegment(
        local_track_id="p1",
        label="person",
        start_time_ms=0,
        end_time_ms=100,
        boxes_by_time_ms={0: _BOX, 100: _BOX},
        quality=1.0,
    )
    engine = FakeTrackerEngine(fixed_tracks=(track,))
    ground_truth = (
        GroundTruthTrack(track_id="gt1", label="person", boxes_by_time_ms={0: _BOX, 100: _BOX}),
    )
    detection = ObjectDetection(label="person", confidence=0.9, box=_BOX)
    sample = TrackingBenchmarkSample(
        sample_id="t1",
        detections_by_time_ms={0: [detection], 100: [detection]},
        ground_truth=ground_truth,
    )
    run = run_tracking_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="bytetrack",
        dataset_id="virat_ground",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="b" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["idf1"] == 1.0
    assert run.metrics["mota"] == 1.0
    assert run.metrics["hota"] is None
    assert run.task.value == "tracking"


def test_tracking_benchmark_categorizes_a_marker_labelled_failure_safely() -> None:
    engine = FakeTrackerEngine()
    bad_detection = ObjectDetection(label=TRACKER_FAKE_UNREADABLE_LABEL, confidence=0.5, box=_BOX)
    sample = TrackingBenchmarkSample(sample_id="tbad", detections_by_time_ms={0: [bad_detection]})
    run = run_tracking_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="bytetrack",
        dataset_id="virat_ground",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="b" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_failure_count_unreadable"] == 1


def test_tracking_benchmark_execution_failure_marker_is_categorized() -> None:
    engine = FakeTrackerEngine()
    bad_detection = ObjectDetection(label=TRACKER_FAKE_EXEC_FAIL_LABEL, confidence=0.5, box=_BOX)
    sample = TrackingBenchmarkSample(sample_id="tbad2", detections_by_time_ms={0: [bad_detection]})
    run = run_tracking_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="bytetrack",
        dataset_id="virat_ground",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="b" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_failure_count_execution_failure"] == 1


def test_fake_visual_text_engine_produces_a_valid_succeeded_run() -> None:
    recognized = RecognizedText(text="ABC1234", confidence=0.9, box=_BOX)
    engine = FakeVisualTextEngine(fixed_texts=(recognized,))
    sample = VisualTextBenchmarkSample(
        sample_id="v1",
        image=make_solid_frame(),
        reference_text="ABC1234",
        expected_fields={VISUAL_TEXT_FIELD_KEY: "ABC1234"},
    )
    run = run_visual_text_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="paddleocr-lightweight-visual-text",
        dataset_id="ufpr_alpr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="c" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["character_error_rate"] == 0.0
    assert run.metrics["field_extraction_f1"] == 1.0


def test_visual_text_metrics_never_contain_the_raw_recognized_or_reference_text() -> None:
    recognized = RecognizedText(text="ABC1234", confidence=0.9, box=_BOX)
    engine = FakeVisualTextEngine(fixed_texts=(recognized,))
    sample = VisualTextBenchmarkSample(
        sample_id="v1",
        image=make_solid_frame(),
        reference_text="ABC1234",
        expected_fields={VISUAL_TEXT_FIELD_KEY: "ABC1234"},
    )
    run = run_visual_text_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="paddleocr-lightweight-visual-text",
        dataset_id="ufpr_alpr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        inference_config_hash="c" * 64,
        now=_NOW,
    )
    serialized = run.model_dump_json()
    assert "ABC1234" not in serialized


def test_visual_text_benchmark_rejects_an_empty_sample_list() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        run_visual_text_benchmark(
            engine=FakeVisualTextEngine(),
            samples=[],
            candidate_id="paddleocr-lightweight-visual-text",
            dataset_id="ufpr_alpr",
            split_id=SplitId.DEVELOPMENT,
            inference_config={},
            inference_config_hash="c" * 64,
        )


def test_fake_engines_are_interchangeable_through_the_typed_protocols() -> None:
    detector: DetectorEngine = FakeDetectorEngine()
    tracker: TrackerEngine = FakeTrackerEngine()
    visual_text: VisualTextEngine = FakeVisualTextEngine()
    assert detector.detect(make_solid_frame()).backend == "fake-cpu"
    assert tracker.track({}).backend == "fake-cpu"
    assert visual_text.recognize_regions(make_solid_frame()).backend == "fake-cpu"


# --- Canonical observation/provenance compatibility, and track-ID safety --


def test_accepted_detection_output_builds_a_valid_canonical_observation() -> None:
    detection = ObjectDetection(label="person", confidence=0.9, box=_BOX)
    locator = SourceLocator(frame_number=1, time_start_ms=0, time_end_ms=100)
    draft = build_observation_draft_for_detection(
        detection, image_width=64, image_height=64, locator=locator
    )
    observation = observation_for_draft(
        draft,
        case_id=uuid4(),
        evidence_id=uuid4(),
        processor_name="visual_benchmark_yolo11n",
        processor_version="1.0.0",
        model_version="yolo11n-benchmark",
        created_at=_NOW,
    )
    assert observation.observation_type == "object_detection"
    assert observation.source_locator.bbox_xyxy_normalized is not None
    assert observation.extraction_confidence == 0.9


def test_accepted_track_segment_never_becomes_an_entity_or_identity_claim() -> None:
    track = TrackSegment(
        local_track_id="local-track-42",
        label="person",
        start_time_ms=0,
        end_time_ms=100,
        boxes_by_time_ms={0: _BOX},
        quality=1.0,
    )
    locator = SourceLocator(frame_number=1, time_start_ms=0, time_end_ms=100)
    draft = build_observation_draft_for_track_segment(track, locator=locator)
    observation = observation_for_draft(
        draft,
        case_id=uuid4(),
        evidence_id=uuid4(),
        processor_name="visual_benchmark_bytetrack",
        processor_version="1.0.0",
        model_version="bytetrack-benchmark",
        created_at=_NOW,
    )
    # A technical track ID becomes only a discriminator on the observation
    # ID -- never an ExtractedEntityMention, never a person/vehicle identity.
    assert observation.observation_type == "anonymous_track_segment"
    assert observation.extracted_entities == []
    assert "local-track-42" not in [e.text for e in observation.extracted_entities]


def test_accepted_visual_text_output_builds_a_valid_canonical_observation() -> None:
    recognized = RecognizedText(text="ABC1234", confidence=0.85, box=_BOX)
    locator = SourceLocator(frame_number=1, time_start_ms=0, time_end_ms=100)
    draft = build_observation_draft_for_visual_text(
        recognized, image_width=64, image_height=64, locator=locator
    )
    observation = observation_for_draft(
        draft,
        case_id=uuid4(),
        evidence_id=uuid4(),
        processor_name="visual_benchmark_paddleocr",
        processor_version="1.0.0",
        model_version="paddleocr-benchmark",
        created_at=_NOW,
    )
    assert observation.observation_type == "ocr_text_mention"
    assert observation.extracted_entities[0].text == "ABC1234"
    assert observation.extracted_entities[0].entity_type_hint == "visual_text_region"
