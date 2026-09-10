"""Deterministic routing decisions for audio input.

No decision here ever fabricates data: a format this module cannot safely
handle is always `DEFERRED_*`/`UNSUPPORTED_AUDIO_FORMAT`/`INVALID_INPUT`,
never `SUCCEEDED` with invented facts. `worker.py` maps each decision to a
`WorkerResultV1` status (`DEFERRED_*` -> `DEFERRED`; `UNSUPPORTED_AUDIO_
FORMAT`/`INVALID_INPUT` -> `FAILED`).
"""

from __future__ import annotations

from enum import StrEnum

_WAV_MAGIC_RIFF = b"RIFF"
_WAV_MAGIC_WAVE = b"WAVE"
_KNOWN_UNSUPPORTED_EXTENSIONS = frozenset({"mp3", "m4a", "aac", "ogg", "flac", "wma", "opus"})


class AudioRoutingDecision(StrEnum):
    """Every possible outcome of routing one audio-related request."""

    SUPPORTED_METADATA_ONLY = "supported_metadata_only"
    SUPPORTED_TRANSCRIPT_IMPORT = "supported_transcript_import"
    SUPPORTED_DIARIZATION_IMPORT = "supported_diarization_import"
    DEFERRED_REQUIRES_ASR = "deferred_requires_asr"
    DEFERRED_REQUIRES_DIARIZATION = "deferred_requires_diarization"
    UNSUPPORTED_AUDIO_FORMAT = "unsupported_audio_format"
    INVALID_INPUT = "invalid_input"


def _extension_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _sniff_is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[0:4] == _WAV_MAGIC_RIFF and data[8:12] == _WAV_MAGIC_WAVE


def _sniff_known_unsupported(data: bytes) -> bool:
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True  # MP3 (ID3 tag, or a bare frame sync)
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True  # M4A/AAC (ISO base media container)
    return data[:4] == b"OggS"  # OGG


def route_audio_metadata(filename: str, data: bytes) -> AudioRoutingDecision:
    """Decide how to route an `audio_metadata` request.

    Extension and content are cross-checked, the same "don't trust either
    alone" policy `structured_processing.document.classifier` uses: a
    `.wav` extension whose content doesn't actually sniff as RIFF/WAVE is
    `invalid_input`, not silently accepted. A `.wav` extension with no
    bytes at all to sniff yet (e.g. zero-length) is deferred to
    `metadata.extract_wav_metadata`'s own validation.
    """
    extension = _extension_of(filename)

    if extension == "wav":
        if len(data) == 0 or _sniff_is_wav(data):
            return AudioRoutingDecision.SUPPORTED_METADATA_ONLY
        return AudioRoutingDecision.INVALID_INPUT

    if extension in _KNOWN_UNSUPPORTED_EXTENSIONS:
        return AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT

    if extension == "":
        if _sniff_is_wav(data):
            return AudioRoutingDecision.SUPPORTED_METADATA_ONLY
        if _sniff_known_unsupported(data):
            return AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT
        return AudioRoutingDecision.INVALID_INPUT

    return AudioRoutingDecision.INVALID_INPUT
