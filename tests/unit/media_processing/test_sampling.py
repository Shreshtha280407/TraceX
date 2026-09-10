"""Scenarios 4-5: deterministic sampling and sampling-limit enforcement/dedup."""

from __future__ import annotations

import pytest

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.models import SamplingRequest, SamplingStrategy, VideoMetadata
from app.modules.media_processing.video.sampling import build_sample_plan

_METADATA = VideoMetadata(
    container_format="mov,mp4",
    duration_ms=5000,
    width=640,
    height=480,
    frame_rate=30.0,
    frame_count=150,
    video_codec="h264",
    has_audio=False,
    rotation_degrees=None,
)


def test_uniform_interval_is_deterministic() -> None:
    request = SamplingRequest(
        strategy=SamplingStrategy.UNIFORM_INTERVAL, interval_ms=1000, max_frames=50
    )
    first = build_sample_plan(_METADATA, request)
    second = build_sample_plan(_METADATA, request)
    assert [t.time_ms for t in first] == [t.time_ms for t in second]
    assert [t.time_ms for t in first] == [0, 1000, 2000, 3000, 4000]


def test_uniform_interval_indices_are_ascending_and_ordinal() -> None:
    request = SamplingRequest(
        strategy=SamplingStrategy.UNIFORM_INTERVAL, interval_ms=2000, max_frames=50
    )
    plan = build_sample_plan(_METADATA, request)
    assert [t.index for t in plan] == list(range(len(plan)))
    assert [t.time_ms for t in plan] == sorted(t.time_ms for t in plan)


def test_fixed_fps_is_deterministic() -> None:
    request = SamplingRequest(strategy=SamplingStrategy.FIXED_FPS, fps=2.0, max_frames=50)
    first = build_sample_plan(_METADATA, request)
    second = build_sample_plan(_METADATA, request)
    assert [t.time_ms for t in first] == [t.time_ms for t in second]
    assert [t.time_ms for t in first] == [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500]


def test_explicit_timestamps_are_sorted_and_deduplicated() -> None:
    request = SamplingRequest(
        strategy=SamplingStrategy.EXPLICIT_TIMESTAMPS,
        explicit_timestamps_ms=(3000, 1000, 1000, 2000),
        max_frames=50,
    )
    plan = build_sample_plan(_METADATA, request)
    assert [t.time_ms for t in plan] == [1000, 2000, 3000]


def test_sampling_limit_is_enforced_not_truncated() -> None:
    request = SamplingRequest(
        strategy=SamplingStrategy.UNIFORM_INTERVAL, interval_ms=100, max_frames=3
    )
    with pytest.raises(ProcessingError) as excinfo:
        build_sample_plan(_METADATA, request)
    assert excinfo.value.code == ErrorCode.SAMPLING_LIMIT_EXCEEDED


def test_uniform_interval_requires_interval_ms() -> None:
    request = SamplingRequest(strategy=SamplingStrategy.UNIFORM_INTERVAL, max_frames=10)
    with pytest.raises(ProcessingError):
        build_sample_plan(_METADATA, request)


def test_fixed_fps_requires_positive_fps() -> None:
    request = SamplingRequest(strategy=SamplingStrategy.FIXED_FPS, fps=0.0, max_frames=10)
    with pytest.raises(ProcessingError):
        build_sample_plan(_METADATA, request)


def test_zero_duration_video_still_samples_at_least_one_frame() -> None:
    zero_duration = VideoMetadata(
        container_format="mov,mp4",
        duration_ms=0,
        width=640,
        height=480,
        frame_rate=30.0,
        frame_count=1,
        video_codec="h264",
        has_audio=False,
        rotation_degrees=None,
    )
    request = SamplingRequest(
        strategy=SamplingStrategy.UNIFORM_INTERVAL, interval_ms=1000, max_frames=10
    )
    plan = build_sample_plan(zero_duration, request)
    assert [t.time_ms for t in plan] == [0]
