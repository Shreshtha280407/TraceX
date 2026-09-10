"""The communication-processing worker entry point: `WorkerJobV1` + typed
input payload in, `WorkerResultV1` out.

`process_job` is the only function later-phase orchestration code needs to
call. It never touches PostgreSQL, Neo4j, Redis, MinIO, a queue, an HTTP
client, a subprocess, or any ML/ASR/diarization model — everything it
needs arrives already typed in `input_payload` (see `models.InputPayload`).

**Idempotency**: `process_job` is a pure function of its normalized
inputs. Given the same `job` (specifically `case_id`, `evidence_id`,
`processor_name`, `processor_version`) and the same `input_payload`, every
emitted `ObservationV1.observation_id` is identical across repeated calls
(see `provenance.observation_id`) — re-running a job (a retry, a replay)
never produces duplicate-but-differently-identified observations. Nothing
in this module has side effects or mutable shared state between calls.

Only `ProcessingError` is caught here and turned into a `FAILED` result;
any other exception is a programming bug and is allowed to propagate
rather than being silently repackaged as a plausible-looking failure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.contracts.common import SourceLocator
from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.modules.communication_processing.audio.diarization_import import (
    diarization_segments_to_mentions,
)
from app.modules.communication_processing.audio.metadata import extract_wav_metadata
from app.modules.communication_processing.audio.routing import (
    AudioRoutingDecision,
    route_audio_metadata,
)
from app.modules.communication_processing.audio.transcript_import import (
    transcript_segments_to_mentions,
)
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import (
    AudioMetadataInput,
    DiarizationImportInput,
    InputPayload,
    ProcessorProfile,
    RawMention,
    SocialExportInput,
    TranscriptImportInput,
)
from app.modules.communication_processing.provenance import (
    CONFIDENCE_STRUCTURED_COMPLETE,
    mention_to_observation,
)
from app.modules.communication_processing.social.common import chat_message_to_mention
from app.modules.communication_processing.social.instagram import parse_instagram_export
from app.modules.communication_processing.social.json_records import parse_generic_json_export
from app.modules.communication_processing.social.telegram import parse_telegram_export
from app.modules.communication_processing.social.whatsapp import parse_whatsapp_export

AUDIO_METADATA_V1 = ProcessorProfile(
    name="audio_metadata_v1",
    version="1.0.0",
    description="Deterministic WAV technical metadata extraction via the stdlib `wave` module.",
    observation_types=("audio_metadata",),
    confidence_rule="1.00 -- a complete technical fact read directly from a validated WAV header.",
)
TRANSCRIPT_IMPORT_V1 = ProcessorProfile(
    name="transcript_import_v1",
    version="1.0.0",
    description="Typed import of externally-produced transcript segments. Never runs ASR.",
    observation_types=("transcript_segment",),
    confidence_rule="extraction_confidence is the caller-supplied segment confidence, unchanged.",
)
DIARIZATION_IMPORT_V1 = ProcessorProfile(
    name="diarization_import_v1",
    version="1.0.0",
    description="Typed import of externally-produced diarization segments. "
    "Never runs real diarization.",
    observation_types=("diarization_speaker_turn",),
    confidence_rule="extraction_confidence is the caller-supplied segment confidence, unchanged.",
)
WHATSAPP_EXPORT_V1 = ProcessorProfile(
    name="whatsapp_export_v1",
    version="1.0.0",
    description="WhatsApp plain-text export parsing.",
    observation_types=("chat_message",),
    confidence_rule="1.00 -- a complete message record read directly from a validated export.",
)
TELEGRAM_EXPORT_V1 = ProcessorProfile(
    name="telegram_export_v1",
    version="1.0.0",
    description="Telegram Desktop JSON export parsing.",
    observation_types=("chat_message",),
    confidence_rule="1.00 -- a complete message record read directly from a validated export.",
)
INSTAGRAM_EXPORT_V1 = ProcessorProfile(
    name="instagram_export_v1",
    version="1.0.0",
    description="Instagram message JSON export parsing.",
    observation_types=("chat_message",),
    confidence_rule="1.00 -- a complete message record read directly from a validated export.",
)
GENERIC_SOCIAL_JSON_V1 = ProcessorProfile(
    name="generic_social_json_v1",
    version="1.0.0",
    description="Generic social-intelligence JSON record export parsing.",
    observation_types=("chat_message",),
    confidence_rule="1.00 -- a complete message record read directly from a validated export.",
)

_AUDIO_PROFILE_NAMES = frozenset(
    {AUDIO_METADATA_V1.name, TRANSCRIPT_IMPORT_V1.name, DIARIZATION_IMPORT_V1.name}
)
_PLATFORM_TO_PROFILE_NAME = {
    "whatsapp": WHATSAPP_EXPORT_V1.name,
    "telegram": TELEGRAM_EXPORT_V1.name,
    "instagram": INSTAGRAM_EXPORT_V1.name,
    "generic_json": GENERIC_SOCIAL_JSON_V1.name,
}
_PROFILES: dict[str, ProcessorProfile] = {
    profile.name: profile
    for profile in (
        AUDIO_METADATA_V1,
        TRANSCRIPT_IMPORT_V1,
        DIARIZATION_IMPORT_V1,
        WHATSAPP_EXPORT_V1,
        TELEGRAM_EXPORT_V1,
        INSTAGRAM_EXPORT_V1,
        GENERIC_SOCIAL_JSON_V1,
    )
}


def process_job(job: WorkerJobV1, input_payload: InputPayload) -> WorkerResultV1:
    """Process one communication-processing job and return a canonical `WorkerResultV1`.

    `job.processor_name` must be one of the names in this module (`_PROFILES`);
    `job.source_type` must match what that profile requires (`audio` for the
    three audio profiles, `chat` for the four social-export profiles); and
    `input_payload`'s role/platform must match what the profile expects.
    Any mismatch fails safely as a named `ProcessingError` rather than being
    silently reinterpreted.
    """
    completed_at = datetime.now(UTC)
    try:
        profile = _get_profile(job.processor_name)
        _validate_source_type(profile, job.source_type)
        mentions, checkpoint, status = _dispatch(profile, input_payload)
        observations = [
            mention_to_observation(
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                profile=profile,
                mention=mention,
                created_at=completed_at,
            )
            for mention in mentions
        ]
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=status,
            observations=observations,
            derived_artifacts=[],
            checkpoint=checkpoint,
            error=None,
            completed_at=completed_at,
        )
    except ProcessingError as exc:
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.FAILED,
            observations=[],
            derived_artifacts=[],
            checkpoint=None,
            error=WorkerError(code=exc.code, message=exc.message, retryable=exc.retryable),
            completed_at=completed_at,
        )


def _get_profile(name: str) -> ProcessorProfile:
    profile = _PROFILES.get(name)
    if profile is None:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_PROCESSOR, f"'{name}' is not a processor this module owns"
        )
    return profile


def _validate_source_type(profile: ProcessorProfile, source_type: SourceType) -> None:
    if profile.name in _AUDIO_PROFILE_NAMES and source_type is not SourceType.AUDIO:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_SOURCE_TYPE,
            f"profile '{profile.name}' requires source_type=audio",
        )
    if profile.name in _PLATFORM_TO_PROFILE_NAME.values() and source_type is not SourceType.CHAT:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_SOURCE_TYPE, f"profile '{profile.name}' requires source_type=chat"
        )


def _dispatch(
    profile: ProcessorProfile, input_payload: InputPayload
) -> tuple[list[RawMention], str | None, WorkerStatus]:
    if profile.name == AUDIO_METADATA_V1.name:
        return _handle_audio_metadata(input_payload)
    if profile.name == TRANSCRIPT_IMPORT_V1.name:
        return _handle_transcript_import(input_payload)
    if profile.name == DIARIZATION_IMPORT_V1.name:
        return _handle_diarization_import(input_payload)
    if profile.name in _PLATFORM_TO_PROFILE_NAME.values():
        return _handle_social_export(profile, input_payload)
    raise ProcessingError(
        ErrorCode.UNSUPPORTED_PROCESSOR, f"no dispatch logic for '{profile.name}'"
    )


def _handle_audio_metadata(
    input_payload: InputPayload,
) -> tuple[list[RawMention], str | None, WorkerStatus]:
    if not isinstance(input_payload, AudioMetadataInput):
        raise ProcessingError(
            ErrorCode.INPUT_ROLE_MISMATCH, "audio_metadata_v1 requires an audio_metadata input"
        )

    decision = route_audio_metadata(input_payload.filename, input_payload.data)
    if decision is AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_AUDIO_FORMAT,
            "audio format is not supported in this phase; a future approved "
            "media-probe adapter is required",
        )
    if decision is AudioRoutingDecision.INVALID_INPUT:
        raise ProcessingError(
            ErrorCode.INVALID_WAV, "audio input is not a valid, recognizable file"
        )

    metadata = extract_wav_metadata(input_payload.data)
    duration_ms = round(metadata.duration_seconds * 1000)
    mention = RawMention(
        observation_type="audio_metadata",
        text="audio_metadata",
        locator=SourceLocator(time_start_ms=0, time_end_ms=max(duration_ms, 0)),
        confidence=CONFIDENCE_STRUCTURED_COMPLETE,
        attributes={
            "duration_seconds": metadata.duration_seconds,
            "sample_rate": metadata.sample_rate,
            "channels": metadata.channels,
            "sample_width_bytes": metadata.sample_width_bytes,
            "frame_count": metadata.frame_count,
            "codec_description": metadata.codec_description,
        },
    )
    return [mention], None, WorkerStatus.SUCCEEDED


def _handle_transcript_import(
    input_payload: InputPayload,
) -> tuple[list[RawMention], str | None, WorkerStatus]:
    if isinstance(input_payload, AudioMetadataInput):
        return (
            [],
            _defer_checkpoint(AudioRoutingDecision.DEFERRED_REQUIRES_ASR),
            WorkerStatus.DEFERRED,
        )
    if not isinstance(input_payload, TranscriptImportInput):
        raise ProcessingError(
            ErrorCode.INPUT_ROLE_MISMATCH,
            "transcript_import_v1 requires a transcript_segments (or audio_metadata, "
            "to be deferred) input",
        )
    return transcript_segments_to_mentions(input_payload.segments), None, WorkerStatus.SUCCEEDED


def _handle_diarization_import(
    input_payload: InputPayload,
) -> tuple[list[RawMention], str | None, WorkerStatus]:
    if isinstance(input_payload, AudioMetadataInput):
        return (
            [],
            _defer_checkpoint(AudioRoutingDecision.DEFERRED_REQUIRES_DIARIZATION),
            WorkerStatus.DEFERRED,
        )
    if not isinstance(input_payload, DiarizationImportInput):
        raise ProcessingError(
            ErrorCode.INPUT_ROLE_MISMATCH,
            "diarization_import_v1 requires a diarization_segments (or audio_metadata, "
            "to be deferred) input",
        )
    return diarization_segments_to_mentions(input_payload.segments), None, WorkerStatus.SUCCEEDED


def _handle_social_export(
    profile: ProcessorProfile, input_payload: InputPayload
) -> tuple[list[RawMention], str | None, WorkerStatus]:
    if not isinstance(input_payload, SocialExportInput):
        raise ProcessingError(
            ErrorCode.INPUT_ROLE_MISMATCH, f"'{profile.name}' requires a social_export input"
        )
    expected_platform = next(
        platform
        for platform, profile_name in _PLATFORM_TO_PROFILE_NAME.items()
        if profile_name == profile.name
    )
    if input_payload.platform != expected_platform:
        raise ProcessingError(
            ErrorCode.INPUT_ROLE_MISMATCH,
            f"'{profile.name}' requires platform='{expected_platform}'",
        )

    if profile.name == WHATSAPP_EXPORT_V1.name:
        records = parse_whatsapp_export(input_payload.data)
    elif profile.name == TELEGRAM_EXPORT_V1.name:
        records = parse_telegram_export(input_payload.data)
    elif profile.name == INSTAGRAM_EXPORT_V1.name:
        records = parse_instagram_export(input_payload.data)
    else:
        records = parse_generic_json_export(input_payload.data)

    mentions = [chat_message_to_mention(record) for record in records]
    return mentions, None, WorkerStatus.SUCCEEDED


def _defer_checkpoint(reason: AudioRoutingDecision) -> str:
    return json.dumps({"reason": reason.value}, sort_keys=True)


__all__ = [
    "AUDIO_METADATA_V1",
    "DIARIZATION_IMPORT_V1",
    "GENERIC_SOCIAL_JSON_V1",
    "INSTAGRAM_EXPORT_V1",
    "TELEGRAM_EXPORT_V1",
    "TRANSCRIPT_IMPORT_V1",
    "WHATSAPP_EXPORT_V1",
    "process_job",
]
