"""Bounded local WAV → VAD → ASR/diarization execution for Phase 4.

This module owns no persistence or network client.  It receives bytes already
resolved through the authenticated worker boundary and returns only typed,
canonical-observation-ready mentions.  The command adapters are optional:
when ASR is not configured the outcome is deliberately deferred instead of
inventing text.
"""

from __future__ import annotations

import audioop
import io
import wave
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import UUID

from pydantic import JsonValue

from app.contracts.worker import WorkerStatus
from app.modules.communication_processing.audio.asr_adapter import AsrAdapter, AsrAdapterState
from app.modules.communication_processing.audio.diarization_adapter import (
    SEGMENT_SOURCE_MODEL_DERIVED,
    DiarizationAdapter,
    DiarizationAdapterState,
)
from app.modules.communication_processing.audio.diarization_import import (
    diarization_segments_to_mentions,
)
from app.modules.communication_processing.audio.transcript_import import (
    transcript_segments_to_mentions,
)
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import AudioMetadataInput, RawMention
from app.modules.communication_processing.phase4 import (
    AudioProcessingProfile,
    DiarizationDecision,
    SpeechInterval,
    diarization_decision,
    normalize_vad_intervals,
)
from app.modules.communication_processing.signal_validation import AudioChunkScope

_TARGET_SAMPLE_RATE = 16_000
_TARGET_SAMPLE_WIDTH = 2
_TARGET_CHANNELS = 1


@dataclass(frozen=True)
class DecodedAudio:
    pcm_wav: bytes
    duration_ms: int
    sample_rate: int


@dataclass(frozen=True)
class LocalAudioOutcome:
    mentions: list[RawMention]
    status: WorkerStatus
    checkpoint: str | None


def decode_and_resample_wav(data: bytes) -> DecodedAudio:
    """Decode PCM WAV to deterministic 16kHz mono signed-16-bit WAV.

    Unsupported/corrupt input is an explicit processing error; no ffmpeg
    fallback is hidden here because the local bridge contract deliberately
    accepts only a bounded, auditable PCM WAV input.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            compression = reader.getcomptype()
            frames = reader.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise ProcessingError(
            ErrorCode.INVALID_WAV, "local audio input is not a valid PCM WAV"
        ) from exc
    if (
        compression != "NONE"
        or channels not in {1, 2}
        or width not in {1, 2, 3, 4}
        or sample_rate <= 0
    ):
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_AUDIO_FORMAT, "audio format is not supported by local PCM decode"
        )
    try:
        if channels == 2:
            frames = audioop.tomono(frames, width, 0.5, 0.5)
        if width != _TARGET_SAMPLE_WIDTH:
            frames = audioop.lin2lin(frames, width, _TARGET_SAMPLE_WIDTH)
        if sample_rate != _TARGET_SAMPLE_RATE:
            frames, _ = audioop.ratecv(
                frames,
                _TARGET_SAMPLE_WIDTH,
                _TARGET_CHANNELS,
                sample_rate,
                _TARGET_SAMPLE_RATE,
                None,
            )
    except audioop.error as exc:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_AUDIO_FORMAT, "audio could not be normalized safely"
        ) from exc
    duration_ms = round(frame_count * 1000 / sample_rate)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(_TARGET_CHANNELS)
        writer.setsampwidth(_TARGET_SAMPLE_WIDTH)
        writer.setframerate(_TARGET_SAMPLE_RATE)
        writer.writeframes(frames)
    return DecodedAudio(
        pcm_wav=output.getvalue(), duration_ms=duration_ms, sample_rate=_TARGET_SAMPLE_RATE
    )


def energy_vad_intervals(
    audio: DecodedAudio, profile: AudioProcessingProfile
) -> tuple[SpeechInterval, ...]:
    """Use a fixed energy threshold to produce deterministic VAD candidates.

    The threshold is intentionally conservative and is provenance-recorded by
    the caller profile; it is a gating signal, not speaker or language
    inference. Silence is a valid empty result.
    """
    with wave.open(io.BytesIO(audio.pcm_wav), "rb") as reader:
        frame_width = reader.getsampwidth() * reader.getnchannels()
        frames_per_window = max(1, reader.getframerate() * profile.vad_frame_ms // 1000)
        window_bytes = frames_per_window * frame_width
        raw = reader.readframes(reader.getnframes())
    active: list[SpeechInterval] = []
    for offset in range(0, len(raw), window_bytes):
        chunk = raw[offset : offset + window_bytes]
        if not chunk:
            continue
        start_ms = offset * 1000 // (frame_width * audio.sample_rate)
        end_ms = min(
            audio.duration_ms, (offset + len(chunk)) * 1000 // (frame_width * audio.sample_rate)
        )
        if end_ms <= start_ms:
            continue
        if audioop.rms(chunk, _TARGET_SAMPLE_WIDTH) >= profile.vad_energy_threshold:
            active.append(SpeechInterval(start_ms, end_ms))
    return normalize_vad_intervals(tuple(active), profile)


def audio_window(audio: DecodedAudio, interval: SpeechInterval) -> bytes:
    """Return one source-relative VAD window as PCM WAV without time repair."""
    with wave.open(io.BytesIO(audio.pcm_wav), "rb") as reader:
        frame_rate = reader.getframerate()
        start = interval.start_ms * frame_rate // 1000
        end = interval.end_ms * frame_rate // 1000
        reader.setpos(start)
        frames = reader.readframes(max(0, end - start))
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(_TARGET_CHANNELS)
        writer.setsampwidth(_TARGET_SAMPLE_WIDTH)
        writer.setframerate(_TARGET_SAMPLE_RATE)
        writer.writeframes(frames)
    return output.getvalue()


def process_local_audio(
    *,
    payload: AudioMetadataInput,
    profile: AudioProcessingProfile,
    asr_adapter: AsrAdapter,
    diarization_adapter: DiarizationAdapter,
    manifest_id: UUID,
    chunk_ids: tuple[UUID, ...],
) -> LocalAudioOutcome:
    """Run one bounded local-audio job; preserve successful ASR if diarization fails."""
    decoded = decode_and_resample_wav(payload.data)
    intervals = _split_at_chunk_boundaries(energy_vad_intervals(decoded, profile), profile)
    if asr_adapter.state is not AsrAdapterState.READY:
        return LocalAudioOutcome([], WorkerStatus.DEFERRED, "local_asr_unavailable")
    mentions: list[RawMention] = []
    if not intervals:
        return LocalAudioOutcome([], WorkerStatus.SUCCEEDED, None)
    # This worker accepts exactly one claimed bounded audio object today.
    # Its profile chunk duration bounds each ASR input; chunk identity is
    # deterministic and kept as safe lineage on every produced observation.
    for interval_index, interval in enumerate(intervals):
        try:
            asr = asr_adapter.transcribe(audio_window(decoded, interval), filename=payload.filename)
        except ProcessingError:
            return LocalAudioOutcome(mentions, WorkerStatus.DEFERRED, "local_asr_chunk_deferred")
        shifted = tuple(
            replace(
                segment,
                start_ms=segment.start_ms + interval.start_ms,
                end_ms=segment.end_ms + interval.start_ms,
                source_segment_id=f"chunk-{interval_index}:{segment.source_segment_id}",
            )
            for segment in asr.segments
        )
        chunk_id = chunk_ids[
            min(interval.start_ms // profile.chunk_duration_ms, len(chunk_ids) - 1)
        ]
        common: dict[str, JsonValue] = {
            "manifest_id": str(manifest_id),
            "chunk_id": str(chunk_id),
            "audio_profile": profile.name,
            "audio_profile_config_hash": profile.config_hash(),
            "asr_backend": asr.backend_name,
            "asr_backend_version": asr.backend_version,
            "asr_model_name": asr.model_name,
            "asr_model_version": asr.model_version,
            "asr_model_identity_hash": asr.model_identity_hash,
            "asr_configuration_hash": asr.config_hash,
            "chunk_time_start_ms": interval.start_ms,
            "chunk_time_end_ms": interval.end_ms,
        }
        segment_mentions = transcript_segments_to_mentions(
            shifted,
            json_path_prefix=f"$.phase4.chunks[{interval_index}].asr_segments",
            provenance_attributes=common,
            chunk_scope=AudioChunkScope(
                start_ms=interval.start_ms,
                end_ms=interval.end_ms,
                manifest_id=str(manifest_id),
                chunk_id=str(chunk_id),
            ),
        )
        mentions.extend(
            replace(
                mention,
                extractor_config_hash=asr.config_hash,
                extractor_model_version=f"{asr.model_name}:{asr.model_version}",
            )
            for mention in segment_mentions
        )

    decision = diarization_decision(
        profile=profile,
        intervals=intervals,
        adapter_ready=diarization_adapter.state is DiarizationAdapterState.READY,
    )
    if decision is not DiarizationDecision.RUN:
        return LocalAudioOutcome(mentions, WorkerStatus.SUCCEEDED, None)
    try:
        diarization = diarization_adapter.diarize(decoded.pcm_wav, filename=payload.filename)
    except ProcessingError:
        # ASR observations remain valid and will be published; do not turn a
        # technical speaker-label failure into a fabricated identity claim.
        return LocalAudioOutcome(mentions, WorkerStatus.SUCCEEDED, None)
    diar_common: dict[str, JsonValue] = {
        "manifest_id": str(manifest_id),
        "audio_profile": profile.name,
        "audio_profile_config_hash": profile.config_hash(),
        "diarization_backend": diarization.backend_name,
        "diarization_backend_version": diarization.backend_version,
        "diarization_model_name": diarization.model_name,
        "diarization_model_version": diarization.model_version,
        "diarization_model_identity_hash": diarization.model_identity_hash,
        "diarization_configuration_hash": diarization.config_hash,
        "segment_source": SEGMENT_SOURCE_MODEL_DERIVED,
        "speaker_identity_status": "source_local_unresolved",
    }
    skipped_outside_chunk = False
    for index, segment in enumerate(diarization.segments):
        chunk_index = segment.start_ms // profile.chunk_duration_ms
        # An end exactly on a boundary belongs to the preceding source chunk.
        end_chunk_index = max(segment.end_ms - 1, 0) // profile.chunk_duration_ms
        if (
            chunk_index != end_chunk_index
            or chunk_index >= len(chunk_ids)
            or segment.end_ms <= segment.start_ms
        ):
            # Do not attach an arbitrary chunk identifier to a crossing or
            # malformed turn. Valid ASR output above remains publishable.
            skipped_outside_chunk = True
            continue
        chunk_start = chunk_index * profile.chunk_duration_ms
        chunk_end = min((chunk_index + 1) * profile.chunk_duration_ms, decoded.duration_ms)
        turn_common: dict[str, JsonValue] = {
            **diar_common,
            "chunk_id": str(chunk_ids[chunk_index]),
            "chunk_time_start_ms": chunk_start,
            "chunk_time_end_ms": chunk_end,
        }
        turn_mentions = diarization_segments_to_mentions(
            (segment,),
            json_path_prefix=f"$.phase4.diarization_segments[{index}]",
            provenance_attributes=turn_common,
            chunk_scope=AudioChunkScope(
                start_ms=chunk_start,
                end_ms=chunk_end,
                manifest_id=str(manifest_id),
                chunk_id=str(chunk_ids[chunk_index]),
            ),
        )
        mentions.extend(
            replace(
                mention,
                extractor_config_hash=diarization.config_hash,
                extractor_model_version=f"{diarization.model_name}:{diarization.model_version}",
            )
            for mention in turn_mentions
        )
    return LocalAudioOutcome(
        mentions,
        WorkerStatus.SUCCEEDED,
        "local_diarization_turn_outside_chunk" if skipped_outside_chunk else None,
    )


def _split_at_chunk_boundaries(
    intervals: tuple[SpeechInterval, ...], profile: AudioProcessingProfile
) -> tuple[SpeechInterval, ...]:
    """Keep every ASR call inside one immutable profile chunk boundary."""
    bounded: list[SpeechInterval] = []
    for interval in intervals:
        start = interval.start_ms
        while start < interval.end_ms:
            chunk_end = ((start // profile.chunk_duration_ms) + 1) * profile.chunk_duration_ms
            end = min(interval.end_ms, chunk_end)
            bounded.append(SpeechInterval(start, end))
            start = end
    return tuple(bounded)


def transcript_hash(text: str) -> str:
    """Expose a safe content commitment for tests/integration metadata only."""
    return sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DecodedAudio",
    "LocalAudioOutcome",
    "audio_window",
    "decode_and_resample_wav",
    "energy_vad_intervals",
    "process_local_audio",
]
