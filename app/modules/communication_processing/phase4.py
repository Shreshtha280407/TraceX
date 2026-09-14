"""Phase 4 bounded local-audio planning, VAD, and diarization policy helpers.

No model is imported or downloaded here. A configured local adapter supplies
energy/speech decisions and ASR/diarization output through the existing typed
adapter interfaces; this module makes the time/provenance policy reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.canonical import canonical_sha256
from app.modules.evidence_lifecycle.media_orchestration import (
    ChunkBoundary,
    ChunkManifest,
    ChunkSpec,
    build_manifest,
)


@dataclass(frozen=True)
class AudioProcessingProfile:
    name: str
    version: str
    chunk_duration_ms: int
    vad_frame_ms: int
    vad_padding_ms: int
    vad_merge_gap_ms: int
    vad_energy_threshold: int
    min_speech_ms: int
    max_speech_ms: int
    diarization_min_speech_ms: int
    enable_diarization: bool

    def __post_init__(self) -> None:
        if (
            min(self.chunk_duration_ms, self.vad_frame_ms, self.min_speech_ms, self.max_speech_ms)
            <= 0
        ):
            raise ValueError("audio profile durations must be positive")
        if self.min_speech_ms > self.max_speech_ms:
            raise ValueError("minimum speech duration must not exceed maximum")

    def config_hash(self) -> str:
        return canonical_sha256({"phase4_audio_profile": self.__dict__})


RAPID_AUDIO_PROFILE = AudioProcessingProfile(
    "rapid", "1.0.0", 60_000, 30, 100, 250, 250, 300, 30_000, 60_000, False
)
DEEP_AUDIO_PROFILE = AudioProcessingProfile(
    "deep", "1.0.0", 30_000, 20, 150, 350, 180, 200, 45_000, 20_000, True
)


@dataclass(frozen=True)
class SpeechInterval:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("speech interval must be a non-empty ordered source-relative range")


class DiarizationDecision(StrEnum):
    RUN = "run"
    SKIP_DISABLED = "skip_disabled"
    SKIP_INSUFFICIENT_SPEECH = "skip_insufficient_speech"
    UNAVAILABLE = "unavailable"


def normalize_vad_intervals(
    intervals: tuple[SpeechInterval, ...], profile: AudioProcessingProfile
) -> tuple[SpeechInterval, ...]:
    """Sort, merge bounded gaps, then split long intervals without repairing invalid input."""
    ordered = sorted(intervals, key=lambda value: (value.start_ms, value.end_ms))
    merged: list[SpeechInterval] = []
    for value in ordered:
        if merged and value.start_ms < merged[-1].end_ms:
            raise ValueError("VAD intervals must not overlap")
        if merged and value.start_ms - merged[-1].end_ms <= profile.vad_merge_gap_ms:
            prior = merged.pop()
            merged.append(SpeechInterval(prior.start_ms, value.end_ms))
        else:
            merged.append(value)
    output: list[SpeechInterval] = []
    for value in merged:
        if value.end_ms - value.start_ms < profile.min_speech_ms:
            continue
        start = value.start_ms
        while start < value.end_ms:
            end = min(start + profile.max_speech_ms, value.end_ms)
            if end - start >= profile.min_speech_ms:
                output.append(SpeechInterval(start, end))
            start = end
    return tuple(output)


def diarization_decision(
    *, profile: AudioProcessingProfile, intervals: tuple[SpeechInterval, ...], adapter_ready: bool
) -> DiarizationDecision:
    if not profile.enable_diarization:
        return DiarizationDecision.SKIP_DISABLED
    if sum(item.end_ms - item.start_ms for item in intervals) < profile.diarization_min_speech_ms:
        return DiarizationDecision.SKIP_INSUFFICIENT_SPEECH
    return DiarizationDecision.RUN if adapter_ready else DiarizationDecision.UNAVAILABLE


def detect_script_hint(text: str) -> tuple[str, float]:
    """Deterministic Unicode-range hint; unknown is explicit, never guessed language."""
    letters = [value for value in text if value.isalpha()]
    if not letters:
        return "unknown", 0.0
    ranges = {
        "devanagari": (0x0900, 0x097F),
        "gurmukhi": (0x0A00, 0x0A7F),
        "latin": (0x0041, 0x024F),
    }
    best, count = "unknown", 0
    for name, (low, high) in ranges.items():
        found = sum(low <= ord(value) <= high for value in letters)
        if found > count:
            best, count = name, found
    return (best if count else "unknown"), count / len(letters)


def plan_audio_manifest(
    *,
    case_id: UUID,
    evidence_id: UUID,
    job_id: UUID,
    input_object_uri: str,
    source_type: str,
    processor_name: str,
    processor_version: str,
    duration_ms: int,
    profile: AudioProcessingProfile,
    created_at: datetime,
) -> ChunkManifest:
    if duration_ms < 0:
        raise ValueError("audio duration must not be negative")
    chunks = tuple(
        ChunkSpec(
            index=index,
            boundary=ChunkBoundary(
                time_start_ms=start, time_end_ms=min(start + profile.chunk_duration_ms, duration_ms)
            ),
        )
        for index, start in enumerate(range(0, max(duration_ms, 1), profile.chunk_duration_ms))
    )
    return build_manifest(
        case_id=case_id,
        evidence_id=evidence_id,
        job_id=job_id,
        source_type=source_type,
        processor_name=processor_name,
        processor_version=processor_version,
        input_object_uri=input_object_uri,
        configuration_hash=profile.config_hash(),
        chunks=chunks,
        created_at=created_at,
    )


__all__ = [
    "DEEP_AUDIO_PROFILE",
    "RAPID_AUDIO_PROFILE",
    "AudioProcessingProfile",
    "DiarizationDecision",
    "SpeechInterval",
    "detect_script_hint",
    "diarization_decision",
    "normalize_vad_intervals",
    "plan_audio_manifest",
]
