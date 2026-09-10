"""Scenario 3: ffprobe result parsing with valid, malformed, and missing fields.

`subprocess.run` is monkeypatched throughout -- probe parsing is pure logic
and must not depend on a real `ffprobe` binary being on `PATH` to be a
"unit" test (the real-binary path is covered by
`tests/integration/media_processing/`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
from app.modules.media_processing.video import probe as probe_module

_SENTINEL_PATH = Path("/tmp/definitely-not-a-real-path-marker-xyz")


def _fake_completed(
    payload: dict[str, Any] | None, *, returncode: int = 0
) -> subprocess.CompletedProcess[bytes]:
    stdout = json.dumps(payload).encode() if payload is not None else b"not json"
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


def _valid_payload(**stream_overrides: Any) -> dict[str, Any]:
    stream = {
        "codec_type": "video",
        "width": 640,
        "height": 480,
        "avg_frame_rate": "30/1",
        "r_frame_rate": "30/1",
        "nb_frames": "150",
        "codec_name": "h264",
    }
    stream.update(stream_overrides)
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "5.000000"},
        "streams": [stream],
    }


def test_probe_video_raises_when_ffprobe_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: None)
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.FFPROBE_UNAVAILABLE


def test_probe_video_parses_a_valid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(_valid_payload())
    )
    metadata = probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.width == 640
    assert metadata.height == 480
    assert metadata.duration_ms == 5000
    assert metadata.frame_rate == pytest.approx(30.0)
    assert metadata.frame_count == 150
    assert metadata.video_codec == "h264"
    assert metadata.has_audio is False
    assert metadata.rotation_degrees is None


def test_probe_video_detects_audio_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload()
    payload["streams"].append({"codec_type": "audio"})
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    metadata = probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.has_audio is True


def test_probe_video_raises_on_nonzero_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(None, returncode=1)
    )
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_PROBE_FAILED


def test_probe_video_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(None))
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_PROBE_FAILED


def test_probe_video_raises_when_no_video_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = {"format": {"duration": "1.0"}, "streams": [{"codec_type": "audio"}]}
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_PROBE_FAILED


def test_probe_video_raises_when_dimensions_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload()
    del payload["streams"][0]["width"]
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_PROBE_FAILED


def test_probe_video_raises_when_duration_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload()
    del payload["format"]["duration"]
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_PROBE_FAILED


def test_probe_video_handles_zero_over_zero_frame_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload(avg_frame_rate="0/0", r_frame_rate="0/0")
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    metadata = probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.frame_rate is None


def test_probe_video_missing_frame_count_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload()
    del payload["streams"][0]["nb_frames"]
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    metadata = probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.frame_count is None


def test_probe_video_reads_rotation_from_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload(tags={"rotate": "90"})
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    metadata = probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.rotation_degrees == 90


def test_probe_video_reads_rotation_from_side_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    payload = _valid_payload(
        side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90.0}]
    )
    monkeypatch.setattr(probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(payload))
    metadata = probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert metadata.rotation_degrees == -90


def test_probe_video_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")

    def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=5)

    monkeypatch.setattr(probe_module.subprocess, "run", _raise_timeout)
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert excinfo.value.code == ErrorCode.MEDIA_PROBE_FAILED


def test_probe_video_error_message_never_contains_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        probe_module.subprocess, "run", lambda *a, **kw: _fake_completed(None, returncode=1)
    )
    with pytest.raises(ProcessingError) as excinfo:
        probe_module.probe_video(_SENTINEL_PATH, limits=DEFAULT_MEDIA_LIMITS)
    assert str(_SENTINEL_PATH) not in excinfo.value.message
