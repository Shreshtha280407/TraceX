"""Scenarios 1-2: valid WAV metadata extraction; malformed/truncated/oversized safe failure."""

from __future__ import annotations

import pytest

from app.modules.communication_processing.audio import metadata as metadata_module
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from tests.fixtures.communication_processing.builders import build_wav_bytes, truncate_wav_bytes


def test_valid_wav_extracts_deterministic_metadata() -> None:
    data = build_wav_bytes(duration_seconds=2.0, sample_rate=8000, channels=1, sample_width=2)
    result = metadata_module.extract_wav_metadata(data)

    assert result.sample_rate == 8000
    assert result.channels == 1
    assert result.sample_width_bytes == 2
    assert result.frame_count == 16000
    assert result.duration_seconds == pytest.approx(2.0)
    assert result.codec_description == "PCM"


def test_wav_extraction_is_deterministic() -> None:
    data = build_wav_bytes(duration_seconds=0.5)
    first = metadata_module.extract_wav_metadata(data)
    second = metadata_module.extract_wav_metadata(data)
    assert first == second


def test_non_riff_bytes_fail_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        metadata_module.extract_wav_metadata(b"not a wav file at all, just junk bytes")
    assert exc_info.value.code == ErrorCode.INVALID_WAV


def test_truncated_wav_fails_safely() -> None:
    full = build_wav_bytes(duration_seconds=1.0, sample_rate=8000, channels=1, sample_width=2)
    truncated = truncate_wav_bytes(full, keep_bytes=60)  # header intact, data chunk cut short
    with pytest.raises(ProcessingError) as exc_info:
        metadata_module.extract_wav_metadata(truncated)
    assert exc_info.value.code == ErrorCode.INVALID_WAV


def test_oversized_wav_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata_module, "MAX_AUDIO_BYTES", 10)
    data = build_wav_bytes(duration_seconds=0.1)
    with pytest.raises(ProcessingError) as exc_info:
        metadata_module.extract_wav_metadata(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_overlong_duration_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata_module, "MAX_AUDIO_DURATION_SECONDS", 0.5)
    data = build_wav_bytes(duration_seconds=1.0)
    with pytest.raises(ProcessingError) as exc_info:
        metadata_module.extract_wav_metadata(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_error_message_never_contains_a_file_path_or_traceback() -> None:
    try:
        metadata_module.extract_wav_metadata(b"junk")
    except ProcessingError as exc:
        assert "Traceback" not in exc.message
        assert ".py" not in exc.message
