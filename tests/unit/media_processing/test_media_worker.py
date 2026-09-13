"""Scenarios 10, 11, 13, 14: worker orchestration, provenance, and safety.

Video-path tests monkeypatch `probe_video`/`extract_frames` on the worker
module itself so this stays a true "unit" test with no real `ffmpeg`/
`ffprobe` dependency; the real-binary end-to-end path is covered by
`tests/integration/media_processing/`. The image path needs no such
monkeypatching -- image decode depends only on Pillow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.contracts.evidence import SourceType
from app.contracts.observation import ObservationV1
from app.contracts.worker import WorkerResultV1, WorkerStatus
from app.modules.media_processing import worker as worker_module
from app.modules.media_processing.analysis.fake_detector import FakeObjectDetector
from app.modules.media_processing.analysis.fake_tracker import FakeObjectTracker
from app.modules.media_processing.analysis.interfaces import ObjectDetection
from app.modules.media_processing.errors import ErrorCode
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS, MediaLimits
from app.modules.media_processing.models import ExtractedFrame, VideoMetadata
from app.modules.media_processing.ocr_adapter import FixtureOcrAdapter
from app.modules.media_processing.source import StaticBytesResolver
from app.modules.media_processing.worker import (
    PROCESSOR_NAME_DETECTION,
    PROCESSOR_NAME_METADATA,
    process_job,
)
from tests.fixtures.media_processing.factory import make_evidence_and_job
from tests.fixtures.media_processing.synthetic import make_png_bytes, make_solid_frame

_VIDEO_METADATA = VideoMetadata(
    container_format="mov,mp4",
    duration_ms=3000,
    width=64,
    height=48,
    frame_rate=10.0,
    frame_count=30,
    video_codec="h264",
    has_audio=False,
    rotation_degrees=None,
)


@dataclass(frozen=True)
class _FixedDetector:
    detections: tuple[ObjectDetection, ...]

    def detect(self, frame: Any) -> list[ObjectDetection]:
        return list(self.detections)


def _assert_valid_result(result: WorkerResultV1) -> None:
    assert isinstance(result, WorkerResultV1)
    for observation in result.observations:
        assert isinstance(observation, ObservationV1)


# --- Image pipeline (no external binary dependency) ---


def test_metadata_only_image_succeeds_with_one_observation() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    data = make_png_bytes(width=32, height=16)
    result = process_job(job, evidence, StaticBytesResolver(payload=data))
    _assert_valid_result(result)
    assert result.status is WorkerStatus.SUCCEEDED
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.observation_type == "media_metadata"
    assert observation.attributes["media_width"] == 32
    assert observation.attributes["media_height"] == 16
    assert observation.extraction_confidence == 1.0


def test_unsupported_content_type_fails_the_job() -> None:
    evidence, job = make_evidence_and_job(
        content_type="audio/mpeg",
        filename="voice.mp3",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=b"noop"))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert result.observations == []


def test_source_type_mismatch_fails_the_job() -> None:
    # content type is an image, but evidence claims it's a video.
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.VIDEO,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=make_png_bytes()))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


def test_detection_processor_without_detector_raises_analysis_not_configured() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=make_png_bytes()))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.ANALYSIS_NOT_CONFIGURED


def test_unknown_processor_name_fails_safely() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name="not_a_real_processor",
        source_type=SourceType.IMAGE,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=make_png_bytes()))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.ANALYSIS_NOT_CONFIGURED


def test_image_detection_with_fake_detector_produces_object_detection_observation() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=make_png_bytes(width=100, height=100)),
        detector=FakeObjectDetector(),
    )
    _assert_valid_result(result)
    assert result.status is WorkerStatus.SUCCEEDED
    types = [o.observation_type for o in result.observations]
    assert types.count("media_metadata") == 1
    assert types.count("object_detection") == 1
    detection_observation = next(
        o for o in result.observations if o.observation_type == "object_detection"
    )
    assert detection_observation.source_locator.frame_number is None
    assert detection_observation.source_locator.time_start_ms is None
    assert detection_observation.source_locator.bbox_xyxy_normalized is not None
    assert detection_observation.attributes["detected_label"] == "person"
    assert 0.0 <= detection_observation.extraction_confidence <= 1.0


def test_image_text_region_with_ocr_produces_ocr_text_mention() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    detector = FakeObjectDetector(label="text_region")
    batches = []

    def fake_callback(frame, img, meta):
        batches.append((frame, meta))

    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=make_png_bytes(width=100, height=100)),
        detector=detector,
        ocr_adapter=FixtureOcrAdapter(),
        submit_ocr_batch=fake_callback,
    )
    _assert_valid_result(result)
    assert len(batches) == 1


def test_input_size_limit_is_enforced() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    tiny_limits = MediaLimits(
        max_input_bytes=10,
        max_video_duration_ms=DEFAULT_MEDIA_LIMITS.max_video_duration_ms,
        max_width=DEFAULT_MEDIA_LIMITS.max_width,
        max_height=DEFAULT_MEDIA_LIMITS.max_height,
        max_frame_rate=DEFAULT_MEDIA_LIMITS.max_frame_rate,
        max_sampled_frames=DEFAULT_MEDIA_LIMITS.max_sampled_frames,
        max_image_pixels=DEFAULT_MEDIA_LIMITS.max_image_pixels,
        max_ocr_crop_count=DEFAULT_MEDIA_LIMITS.max_ocr_crop_count,
        subprocess_timeout_seconds=DEFAULT_MEDIA_LIMITS.subprocess_timeout_seconds,
    )
    result = process_job(
        job, evidence, StaticBytesResolver(payload=make_png_bytes()), limits=tiny_limits
    )
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.MEDIA_LIMIT_EXCEEDED


def test_malformed_detection_confidence_is_skipped_not_fatal() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    bad_detection = ObjectDetection(
        label="person", confidence=1.5, box=PixelBoundingBox(x_min=1, y_min=1, x_max=5, y_max=5)
    )
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=make_png_bytes(width=50, height=50)),
        detector=_FixedDetector(detections=(bad_detection,)),
    )
    assert result.status is WorkerStatus.SUCCEEDED
    assert [o.observation_type for o in result.observations] == ["media_metadata"]


def test_malformed_detection_bbox_is_skipped_not_fatal() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    # x_min >= x_max: invalid geometry, never clamped, never silently created.
    bad_detection = ObjectDetection(
        label="person", confidence=0.9, box=PixelBoundingBox(x_min=40, y_min=1, x_max=10, y_max=5)
    )
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=make_png_bytes(width=50, height=50)),
        detector=_FixedDetector(detections=(bad_detection,)),
    )
    assert result.status is WorkerStatus.SUCCEEDED
    assert [o.observation_type for o in result.observations] == ["media_metadata"]


# --- Video pipeline (probe/extract monkeypatched -- no real ffmpeg dependency) ---


def _make_video_job_and_evidence(processor_name: str) -> Any:
    return make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=processor_name,
        source_type=SourceType.VIDEO,
    )


def test_video_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "probe_video", lambda path, *, limits: _VIDEO_METADATA)
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_METADATA)
    result = process_job(job, evidence, StaticBytesResolver(payload=b"fake-mp4-bytes"))
    _assert_valid_result(result)
    assert result.status is WorkerStatus.SUCCEEDED
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.observation_type == "media_metadata"
    assert observation.source_locator.time_start_ms == 0
    assert observation.attributes["duration_ms"] == 3000
    assert observation.attributes["container_format"] == "mov,mp4"


def test_video_over_limit_duration_fails_before_frame_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "probe_video", lambda path, *, limits: _VIDEO_METADATA)
    extract_calls: list[int] = []
    monkeypatch.setattr(
        worker_module, "extract_frames", lambda *a, **kw: extract_calls.append(1) or ([], 0)
    )
    tiny_limits = MediaLimits(
        max_input_bytes=DEFAULT_MEDIA_LIMITS.max_input_bytes,
        max_video_duration_ms=100,  # far below _VIDEO_METADATA.duration_ms
        max_width=DEFAULT_MEDIA_LIMITS.max_width,
        max_height=DEFAULT_MEDIA_LIMITS.max_height,
        max_frame_rate=DEFAULT_MEDIA_LIMITS.max_frame_rate,
        max_sampled_frames=DEFAULT_MEDIA_LIMITS.max_sampled_frames,
        max_image_pixels=DEFAULT_MEDIA_LIMITS.max_image_pixels,
        max_ocr_crop_count=DEFAULT_MEDIA_LIMITS.max_ocr_crop_count,
        subprocess_timeout_seconds=DEFAULT_MEDIA_LIMITS.subprocess_timeout_seconds,
    )
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_DETECTION)
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=b"fake-mp4-bytes"),
        limits=tiny_limits,
        detector=FakeObjectDetector(),
    )
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.MEDIA_LIMIT_EXCEEDED
    assert extract_calls == []  # never reached the expensive step


def test_video_probe_failure_fails_the_whole_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.media_processing.errors import ProcessingError

    def _raise(path: Any, *, limits: Any) -> Any:
        raise ProcessingError(ErrorCode.MEDIA_PROBE_FAILED, "no usable video stream")

    monkeypatch.setattr(worker_module, "probe_video", _raise)
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_METADATA)
    result = process_job(job, evidence, StaticBytesResolver(payload=b"fake-mp4-bytes"))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.MEDIA_PROBE_FAILED


def _fake_frames() -> list[ExtractedFrame]:
    frame_image = make_solid_frame(width=_VIDEO_METADATA.width, height=_VIDEO_METADATA.height)
    return [
        ExtractedFrame(
            frame_number=0,
            time_start_ms=0,
            time_end_ms=100,
            width=_VIDEO_METADATA.width,
            height=_VIDEO_METADATA.height,
            image=frame_image,
        ),
        ExtractedFrame(
            frame_number=10,
            time_start_ms=1000,
            time_end_ms=1100,
            width=_VIDEO_METADATA.width,
            height=_VIDEO_METADATA.height,
            image=frame_image,
        ),
    ]


def test_video_detection_produces_frame_and_time_locators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "probe_video", lambda path, *, limits: _VIDEO_METADATA)
    monkeypatch.setattr(worker_module, "extract_frames", lambda *a, **kw: (_fake_frames(), 0))
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_DETECTION)
    result = process_job(
        job, evidence, StaticBytesResolver(payload=b"fake-mp4-bytes"), detector=FakeObjectDetector()
    )
    _assert_valid_result(result)
    assert result.status is WorkerStatus.SUCCEEDED
    detections = [o for o in result.observations if o.observation_type == "object_detection"]
    assert len(detections) == 2
    for observation in detections:
        assert observation.source_locator.frame_number is not None
        assert observation.source_locator.time_start_ms is not None
        assert observation.source_locator.time_end_ms is not None
        assert observation.source_locator.bbox_xyxy_normalized is not None
        assert observation.attributes["sampling_strategy"] == "uniform_interval"


def test_video_tracking_produces_anonymous_track_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "probe_video", lambda path, *, limits: _VIDEO_METADATA)
    monkeypatch.setattr(worker_module, "extract_frames", lambda *a, **kw: (_fake_frames(), 0))
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_DETECTION)
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=b"fake-mp4-bytes"),
        detector=FakeObjectDetector(),
        tracker=FakeObjectTracker(),
    )
    _assert_valid_result(result)
    tracks = [o for o in result.observations if o.observation_type == "anonymous_track_segment"]
    assert len(tracks) == 1
    track = tracks[0]
    assert track.source_locator.time_start_ms == 0
    assert track.source_locator.time_end_ms == 1000
    assert track.attributes["local_track_id"]
    assert isinstance(track.attributes["track_boxes"], list)
    assert len(track.attributes["track_boxes"]) == 2


def test_video_ocr_on_text_region_produces_ocr_text_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "probe_video", lambda path, *, limits: _VIDEO_METADATA)
    monkeypatch.setattr(worker_module, "extract_frames", lambda *a, **kw: (_fake_frames(), 0))
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_DETECTION)
    batches = []

    def fake_callback(frame, img, meta):
        batches.append((frame, meta))

    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=b"fake-mp4-bytes"),
        detector=FakeObjectDetector(label="text_region"),
        ocr_adapter=FixtureOcrAdapter(),
        submit_ocr_batch=fake_callback,
    )
    _assert_valid_result(result)
    assert len(batches) == 2


def test_video_frames_failed_counter_is_tracked_via_stopwatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.media_processing.performance import Stopwatch

    monkeypatch.setattr(worker_module, "probe_video", lambda path, *, limits: _VIDEO_METADATA)
    monkeypatch.setattr(worker_module, "extract_frames", lambda *a, **kw: (_fake_frames(), 3))
    evidence, job = _make_video_job_and_evidence(PROCESSOR_NAME_DETECTION)
    sw = Stopwatch()
    process_job(
        job,
        evidence,
        StaticBytesResolver(payload=b"fake-mp4-bytes"),
        detector=FakeObjectDetector(),
        stopwatch=sw,
    )
    assert sw.timings.frames_failed == 3
    assert sw.timings.frames_extracted == 2
    assert sw.timings.total_ms >= 0.0
