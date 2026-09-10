"""WAV audio metadata extraction via the Python standard `wave` module.

Only WAV (PCM, RIFF/WAVE container) is supported. No `ffmpeg`, `ffprobe`,
subprocess, or shell command is ever used — `wave` parses the RIFF/WAVE
header and PCM data directly. Only deterministic technical facts are
extracted (duration, sample rate, channel count, sample width, frame
count, codec description); nothing here infers speech content, speaker
identity, or transcript text — that would be fabrication.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_SECONDS,
)

_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"


@dataclass(frozen=True)
class WavMetadata:
    """Deterministic technical facts read directly from a WAV file's header."""

    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    codec_description: str


def extract_wav_metadata(data: bytes) -> WavMetadata:
    """Extract WAV metadata.

    Raises `input_limit_exceeded` for a file over `MAX_AUDIO_BYTES` or a
    declared duration over `MAX_AUDIO_DURATION_SECONDS`, and `invalid_wav`
    for anything that isn't a well-formed, complete PCM WAV file —
    including a truncated one (a header declaring more frames than the
    file actually contains).
    """
    if len(data) > MAX_AUDIO_BYTES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"audio exceeds the {MAX_AUDIO_BYTES}-byte limit"
        )
    if len(data) < 12 or data[0:4] != _RIFF_MAGIC or data[8:12] != _WAVE_MAGIC:
        raise ProcessingError(ErrorCode.INVALID_WAV, "not a RIFF/WAVE container")

    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            frame_rate = reader.getframerate()
            frame_count = reader.getnframes()
            comp_type = reader.getcomptype()

            if frame_rate <= 0 or channels <= 0 or sample_width <= 0:
                raise ProcessingError(ErrorCode.INVALID_WAV, "WAV header has a non-positive field")

            # Bounded by the MAX_AUDIO_BYTES check above, so reading the
            # declared frames is safe -- and is the only reliable way to
            # detect truncation (a header can declare more frames than the
            # file actually has).
            actual_frames = reader.readframes(frame_count)
    except wave.Error as exc:
        raise ProcessingError(ErrorCode.INVALID_WAV, "WAV header could not be parsed") from exc

    expected_bytes = frame_count * channels * sample_width
    if len(actual_frames) < expected_bytes:
        raise ProcessingError(ErrorCode.INVALID_WAV, "WAV data is truncated")

    duration_seconds = frame_count / frame_rate
    if duration_seconds > MAX_AUDIO_DURATION_SECONDS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"audio duration exceeds the {MAX_AUDIO_DURATION_SECONDS}-second limit",
        )

    # `wave` only ever successfully opens uncompressed PCM WAV (it raises
    # `wave.Error` for any other format), so a successful open is always PCM.
    codec_description = "PCM" if comp_type == "NONE" else comp_type

    return WavMetadata(
        duration_seconds=duration_seconds,
        sample_rate=frame_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        codec_description=codec_description,
    )
