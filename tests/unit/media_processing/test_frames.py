"""Video frame extraction: trustworthy frame numbers, per-frame soft failure.

`subprocess.run` is monkeypatched -- this is a unit test and must not
depend on a real `ffmpeg` binary (see `tests/integration/media_processing/`
for the real-binary path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
from app.modules.media_processing.models import SampleTimestamp, VideoMetadata
from app.modules.media_processing.video import frames as frames_module

_SENTINEL_PATH = Path("/tmp/definitely-not-a-real-path-marker-xyz")

_METADATA_KNOWN_FRAMES = VideoMetadata(
    container_format="mov,mp4",
    duration_ms=5000,
    width=4,
    height=2,
    frame_rate=10.0,
    frame_count=50,
    video_codec="h264",
    has_audio=False,
    rotation_degrees=None,
)

_METADATA_UNKNOWN_FRAME_COUNT = VideoMetadata(
    container_format="mov,mp4",
    duration_ms=5000,
    width=4,
    height=2,
    frame_rate=None,
    frame_count=None,
    video_codec="h264",
    has_audio=False,
    rotation_degrees=None,
)


def _raw_frame(width: int, height: int, value: int = 7) -> bytes:
    return np.full((height, width, 3), value, dtype=np.uint8).tobytes()


def test_extract_frames_raises_when_ffmpeg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: None)
    with pytest.raises(ProcessingError) as excinfo:
        frames_module.extract_frames(
            _SENTINEL_PATH,
            _METADATA_KNOWN_FRAMES,
            [SampleTimestamp(time_ms=0, index=0)],
            limits=DEFAULT_MEDIA_LIMITS,
        )
    assert excinfo.value.code == ErrorCode.FFMPEG_UNAVAILABLE


def test_extract_frames_returns_decoded_frame_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    payload = _raw_frame(_METADATA_KNOWN_FRAMES.width, _METADATA_KNOWN_FRAMES.height)
    monkeypatch.setattr(
        frames_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=b""
        ),
    )
    frames, failed = frames_module.extract_frames(
        _SENTINEL_PATH,
        _METADATA_KNOWN_FRAMES,
        [SampleTimestamp(time_ms=100, index=0)],
        limits=DEFAULT_MEDIA_LIMITS,
    )
    assert failed == 0
    assert len(frames) == 1
    frame = frames[0]
    assert frame.width == _METADATA_KNOWN_FRAMES.width
    assert frame.height == _METADATA_KNOWN_FRAMES.height
    assert frame.time_start_ms == 100
    assert frame.time_end_ms == 200  # + 1000/10fps
    assert frame.image.shape == (_METADATA_KNOWN_FRAMES.height, _METADATA_KNOWN_FRAMES.width, 3)


def test_extract_frames_sets_frame_number_when_trustworthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    payload = _raw_frame(_METADATA_KNOWN_FRAMES.width, _METADATA_KNOWN_FRAMES.height)
    monkeypatch.setattr(
        frames_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=b""
        ),
    )
    frames, _failed = frames_module.extract_frames(
        _SENTINEL_PATH,
        _METADATA_KNOWN_FRAMES,
        [SampleTimestamp(time_ms=1000, index=0)],
        limits=DEFAULT_MEDIA_LIMITS,
    )
    assert frames[0].frame_number == 10  # 1.0s * 10fps


def test_extract_frames_omits_frame_number_when_not_trustworthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    payload = _raw_frame(_METADATA_UNKNOWN_FRAME_COUNT.width, _METADATA_UNKNOWN_FRAME_COUNT.height)
    monkeypatch.setattr(
        frames_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=b""
        ),
    )
    frames, _failed = frames_module.extract_frames(
        _SENTINEL_PATH,
        _METADATA_UNKNOWN_FRAME_COUNT,
        [SampleTimestamp(time_ms=1000, index=0)],
        limits=DEFAULT_MEDIA_LIMITS,
    )
    assert frames[0].frame_number is None
    assert (
        frames[0].time_end_ms == frames[0].time_start_ms
    )  # unknown frame rate -> zero-duration instant


def test_extract_frames_skips_a_failed_timestamp_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        frames_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"error"
        ),
    )
    frames, failed = frames_module.extract_frames(
        _SENTINEL_PATH,
        _METADATA_KNOWN_FRAMES,
        [SampleTimestamp(time_ms=0, index=0)],
        limits=DEFAULT_MEDIA_LIMITS,
    )
    assert frames == []
    assert failed == 1


def test_extract_frames_skips_a_short_payload_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        frames_module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"short", stderr=b""
        ),
    )
    frames, failed = frames_module.extract_frames(
        _SENTINEL_PATH,
        _METADATA_KNOWN_FRAMES,
        [SampleTimestamp(time_ms=0, index=0)],
        limits=DEFAULT_MEDIA_LIMITS,
    )
    assert frames == []
    assert failed == 1


def test_extract_frames_aggregates_success_and_failure_across_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    good_payload = _raw_frame(_METADATA_KNOWN_FRAMES.width, _METADATA_KNOWN_FRAMES.height)
    calls: list[int] = []

    def _fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(1)
        if len(calls) == 2:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=good_payload, stderr=b"")

    monkeypatch.setattr(frames_module.subprocess, "run", _fake_run)
    timestamps = [SampleTimestamp(time_ms=t, index=i) for i, t in enumerate([0, 100, 200])]
    frames, failed = frames_module.extract_frames(
        _SENTINEL_PATH, _METADATA_KNOWN_FRAMES, timestamps, limits=DEFAULT_MEDIA_LIMITS
    )
    assert len(frames) == 2
    assert failed == 1


def test_extract_frames_never_raises_from_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)

    monkeypatch.setattr(frames_module.subprocess, "run", _raise_timeout)
    frames, failed = frames_module.extract_frames(
        _SENTINEL_PATH,
        _METADATA_KNOWN_FRAMES,
        [SampleTimestamp(time_ms=0, index=0)],
        limits=DEFAULT_MEDIA_LIMITS,
    )
    assert frames == []
    assert failed == 1
