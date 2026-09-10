"""End-to-end video pipeline against real `ffprobe`/`ffmpeg` binaries.

Self-skips the whole module if `ffmpeg`/`ffprobe` are genuinely
unavailable -- see `docs/architecture/media-processing-v1.md`. Uses a
tiny, synthetic, pattern-generated MP4 (`lavfi testsrc`) -- no real
footage, no external test-data dependency.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerResultV1, WorkerStatus
from app.modules.media_processing.analysis.fake_detector import FakeObjectDetector
from app.modules.media_processing.analysis.fake_tracker import FakeObjectTracker
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
from app.modules.media_processing.models import SamplingRequest, SamplingStrategy, VideoMetadata
from app.modules.media_processing.source import StaticBytesResolver, temporary_media_file
from app.modules.media_processing.video.frames import extract_frames
from app.modules.media_processing.video.probe import probe_video
from app.modules.media_processing.video.sampling import build_sample_plan
from app.modules.media_processing.worker import (
    PROCESSOR_NAME_DETECTION,
    PROCESSOR_NAME_METADATA,
    process_job,
)
from tests.fixtures.media_processing.factory import make_evidence_and_job
from tests.fixtures.media_processing.synthetic import ffmpeg_available, make_synthetic_mp4_bytes

pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe not available on PATH"
)

_WIDTH, _HEIGHT, _DURATION_S, _FPS = 64, 48, 2.0, 5.0
_SAMPLING = SamplingRequest(
    strategy=SamplingStrategy.UNIFORM_INTERVAL, interval_ms=500, max_frames=20
)


@pytest.fixture(scope="module")
def synthetic_mp4_bytes() -> bytes:
    return make_synthetic_mp4_bytes(
        width=_WIDTH, height=_HEIGHT, duration_seconds=_DURATION_S, fps=_FPS
    )


def _probe_bytes(data: bytes) -> VideoMetadata:
    with temporary_media_file(data, suffix=".mp4") as path:
        return probe_video(path, limits=DEFAULT_MEDIA_LIMITS)


def _observation_ids(result: WorkerResultV1) -> list[object]:
    return [o.observation_id for o in result.observations]


def test_probe_a_small_generated_mp4(synthetic_mp4_bytes: bytes) -> None:
    metadata = _probe_bytes(synthetic_mp4_bytes)
    assert metadata.width == _WIDTH
    assert metadata.height == _HEIGHT
    assert metadata.duration_ms == pytest.approx(int(_DURATION_S * 1000), abs=50)
    assert metadata.frame_count == int(_DURATION_S * _FPS)
    assert metadata.video_codec == "h264"


def test_extract_deterministic_sample_frames(synthetic_mp4_bytes: bytes) -> None:
    with temporary_media_file(synthetic_mp4_bytes, suffix=".mp4") as path:
        metadata = probe_video(path, limits=DEFAULT_MEDIA_LIMITS)
        plan = build_sample_plan(metadata, _SAMPLING)
        first_frames, first_failed = extract_frames(
            path, metadata, plan, limits=DEFAULT_MEDIA_LIMITS
        )
        second_frames, second_failed = extract_frames(
            path, metadata, plan, limits=DEFAULT_MEDIA_LIMITS
        )

    assert first_failed == 0
    assert second_failed == 0
    assert len(first_frames) == len(plan)
    # Deterministic: identical timestamps against the identical file produce
    # identical pixel content and identical extracted-frame metadata.
    for a, b in zip(first_frames, second_frames, strict=True):
        assert a.time_start_ms == b.time_start_ms
        assert a.width == b.width
        assert a.height == b.height
        assert (a.image == b.image).all()


def test_fake_analysis_pipeline_end_to_end(synthetic_mp4_bytes: bytes) -> None:
    evidence, job = make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.VIDEO,
    )
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=synthetic_mp4_bytes),
        sampling=_SAMPLING,
        detector=FakeObjectDetector(),
        tracker=FakeObjectTracker(),
    )
    assert result.status is WorkerStatus.SUCCEEDED
    types = {o.observation_type for o in result.observations}
    assert "media_metadata" in types
    assert "object_detection" in types
    assert "anonymous_track_segment" in types


def test_exact_frame_time_and_bbox_provenance(synthetic_mp4_bytes: bytes) -> None:
    evidence, job = make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.VIDEO,
    )
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=synthetic_mp4_bytes),
        sampling=_SAMPLING,
        detector=FakeObjectDetector(),
    )
    detections = [o for o in result.observations if o.observation_type == "object_detection"]
    assert detections
    metadata = _probe_bytes(synthetic_mp4_bytes)
    expected_times = {t.time_ms for t in build_sample_plan(metadata, _SAMPLING)}
    for observation in detections:
        locator = observation.source_locator
        assert locator.time_start_ms in expected_times
        assert locator.time_end_ms is not None
        assert locator.time_end_ms >= locator.time_start_ms
        assert locator.bbox_xyxy_normalized is not None
        box = locator.bbox_xyxy_normalized
        assert 0.0 <= box.x_min < box.x_max <= 1.0
        assert 0.0 <= box.y_min < box.y_max <= 1.0
        # frame_number is trustworthy here: frame_count and frame_rate are
        # both known for this synthetic constant-frame-rate clip.
        assert locator.frame_number is not None


def test_repeat_processing_creates_the_same_observation_ids(synthetic_mp4_bytes: bytes) -> None:
    evidence, job = make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.VIDEO,
    )
    resolver = StaticBytesResolver(payload=synthetic_mp4_bytes)

    first = process_job(
        job,
        evidence,
        resolver,
        sampling=_SAMPLING,
        detector=FakeObjectDetector(),
        tracker=FakeObjectTracker(),
    )
    second = process_job(
        job,
        evidence,
        resolver,
        sampling=_SAMPLING,
        detector=FakeObjectDetector(),
        tracker=FakeObjectTracker(),
    )

    assert _observation_ids(first) == _observation_ids(second)
    assert len(_observation_ids(first)) > 0


def test_temporary_artifacts_are_cleaned_up(synthetic_mp4_bytes: bytes) -> None:
    before = set(Path(tempfile.gettempdir()).iterdir())
    evidence, job = make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.VIDEO,
    )
    process_job(job, evidence, StaticBytesResolver(payload=synthetic_mp4_bytes))
    after = set(Path(tempfile.gettempdir()).iterdir())
    assert after - before == set()
