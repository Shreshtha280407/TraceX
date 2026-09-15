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

`run_once`/`main` (bottom of this file) are the Phase 2 addition: a
one-shot CLI runner --

    uv run python -m app.modules.communication_processing.worker --once

-- that claims one compatible job through Nipun's internal worker API
(`client.py`), resolves its evidence via the injected
`input_resolver.WorkerInputResolver`, builds the correct typed
`InputPayload` for that job's `processor_name` (`_build_input_payload`),
calls the existing `process_job` above completely unchanged, and submits
the result. No daemon, polling loop, or scheduler -- see
`docs/architecture/communication-processing-worker.md`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog

from app.contracts.common import SourceLocator
from app.contracts.evidence import SourceType
from app.contracts.observation import ObservationV1
from app.contracts.observation_batch import TransformationProvenanceV1
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.core.config import Settings, get_settings
from app.core.ids import deterministic_uuid
from app.modules.communication_processing.audio.asr_adapter import (
    AsrAdapter,
    LocalCommandAsrAdapter,
)
from app.modules.communication_processing.audio.diarization_adapter import (
    DiarizationAdapter,
    LocalCommandDiarizationAdapter,
)
from app.modules.communication_processing.audio.diarization_import import (
    diarization_segments_to_mentions,
    parse_diarization_import_payload,
)
from app.modules.communication_processing.audio.local_pipeline import process_local_audio
from app.modules.communication_processing.audio.metadata import extract_wav_metadata
from app.modules.communication_processing.audio.routing import (
    AudioRoutingDecision,
    route_audio_metadata,
)
from app.modules.communication_processing.audio.transcript_import import (
    parse_transcript_import_payload,
    transcript_segments_to_mentions,
)
from app.modules.communication_processing.batching import (
    build_batch_submission,
    build_progress,
    build_transformation,
    deterministic_batch_id,
)
from app.modules.communication_processing.client import WorkerApiClient
from app.modules.communication_processing.errors import (
    ErrorCode,
    InputResolutionUnavailableError,
    ProcessingError,
    WorkerApiError,
    WorkerAuthenticationError,
)
from app.modules.communication_processing.input_resolver import (
    LiveInputResolver,
    ResolvedInput,
    WorkerInputResolver,
)
from app.modules.communication_processing.models import (
    AudioMetadataInput,
    DiarizationImportInput,
    InputPayload,
    ProcessorProfile,
    RawMention,
    SocialExportInput,
    TranscriptImportInput,
)
from app.modules.communication_processing.phase4 import (
    DEEP_AUDIO_PROFILE,
    RAPID_AUDIO_PROFILE,
    AudioProcessingProfile,
    plan_audio_manifest,
)
from app.modules.communication_processing.provenance import (
    CONFIDENCE_STRUCTURED_COMPLETE,
    mention_to_observation,
    profile_config_hash,
)
from app.modules.communication_processing.signal_validation import (
    CommunicationSignalOutcome,
    validate_chat_signal,
)
from app.modules.communication_processing.social.common import chat_message_to_mention
from app.modules.communication_processing.social.identifiers import extract_mentioned_identifiers
from app.modules.communication_processing.social.instagram import parse_instagram_export
from app.modules.communication_processing.social.json_records import parse_generic_json_export
from app.modules.communication_processing.social.telegram import parse_telegram_export
from app.modules.communication_processing.social.whatsapp import parse_whatsapp_export
from app.modules.evidence_lifecycle.media_orchestration import (
    ChunkManifest,
    MediaChunkPublication,
    chunk_identity,
)

logger = structlog.get_logger(__name__)

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


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

#: The exact `source_type` a real evidence upload must declare for each
#: profile to route to it -- see `evidence_lifecycle/routing.py`'s Phase 2
#: routing fix, which gave `transcript_import_v1`/`diarization_import_v1`/
#: `whatsapp_export_v1`/`telegram_export_v1`/`instagram_export_v1` each
#: their own disjoint `SourceType` (previously only `audio_metadata_v1`/
#: `generic_social_json_v1` had a real route, via the shared `audio`/`chat`
#: values `_validate_source_type` originally checked for the whole group).
_PROFILE_REQUIRED_SOURCE_TYPES: dict[str, SourceType] = {
    AUDIO_METADATA_V1.name: SourceType.AUDIO,
    TRANSCRIPT_IMPORT_V1.name: SourceType.AUDIO_TRANSCRIPT,
    DIARIZATION_IMPORT_V1.name: SourceType.AUDIO_DIARIZATION,
    WHATSAPP_EXPORT_V1.name: SourceType.WHATSAPP_CHAT,
    TELEGRAM_EXPORT_V1.name: SourceType.TELEGRAM_CHAT,
    INSTAGRAM_EXPORT_V1.name: SourceType.INSTAGRAM_CHAT,
    GENERIC_SOCIAL_JSON_V1.name: SourceType.CHAT,
}
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
    `job.source_type` must be the exact `SourceType` that profile's own
    entry in `_PROFILE_REQUIRED_SOURCE_TYPES` requires; and `input_payload`'s
    role/platform must match what the profile expects. Any mismatch fails
    safely as a named `ProcessingError` rather than being silently
    reinterpreted.
    """
    completed_at = datetime.now(UTC)
    try:
        profile = _get_profile(job.processor_name)
        _validate_source_type(profile, job.source_type)
        mentions, checkpoint, status = _dispatch(profile, input_payload)
        observations = _observations_for(job, profile, mentions, completed_at)
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
    required = _PROFILE_REQUIRED_SOURCE_TYPES.get(profile.name)
    if required is not None and source_type is not required:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_SOURCE_TYPE,
            f"profile '{profile.name}' requires source_type={required.value}",
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


def _handle_local_audio(
    *,
    job: WorkerJobV1,
    input_payload: AudioMetadataInput,
    profile: AudioProcessingProfile,
    asr_adapter: AsrAdapter,
    diarization_adapter: DiarizationAdapter,
    manifest: ChunkManifest,
    created_at: datetime,
) -> tuple[list[RawMention], str | None, WorkerStatus]:
    """Extend the existing raw-audio route without changing its job scope.

    `manifest` is already coordinator-persisted by the caller
    (`run_communication_job_with_batches`, via `client.create_media_manifest`)
    before this function ever runs -- unlike before P5-INTEG-COMMUNICATION-001,
    this is no longer a worker-local plan the coordinator never saw. This
    function itself still performs no direct coordinator/database write; it
    only derives chunk identities from the manifest it was given and
    preserves them as safe lineage on every produced observation.
    """
    metadata_mentions, _, _ = _handle_audio_metadata(input_payload)
    outcome = process_local_audio(
        payload=input_payload,
        profile=profile,
        asr_adapter=asr_adapter,
        diarization_adapter=diarization_adapter,
        manifest_id=manifest.manifest_id,
        chunk_ids=tuple(chunk_identity(manifest, item.index) for item in manifest.chunks),
    )
    metadata = metadata_mentions[0]
    metadata_mentions[0] = RawMention(
        observation_type=metadata.observation_type,
        text=metadata.text,
        locator=metadata.locator,
        confidence=metadata.confidence,
        entity_type_hint=metadata.entity_type_hint,
        event_time=metadata.event_time,
        attributes={
            **metadata.attributes,
            "audio_profile": profile.name,
            "audio_profile_config_hash": profile.config_hash(),
            "manifest_id": str(manifest.manifest_id),
            "manifest_hash": manifest.manifest_hash,
            "local_asr_state": asr_adapter.state.value,
            "local_diarization_state": diarization_adapter.state.value,
        },
    )
    return metadata_mentions + outcome.mentions, outcome.checkpoint, outcome.status


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

    mentions: list[RawMention] = []
    for record in records:
        mentions.append(chat_message_to_mention(record))
        # Identifier extraction is permitted only for a complete source
        # record.  The pure helper remains independently testable for
        # parser fixtures, but publication cannot turn an incomplete chat
        # record into a correlation-ready identifier.
        if validate_chat_signal(record).outcome is CommunicationSignalOutcome.ACCEPTED:
            mentions.extend(extract_mentioned_identifiers(record))
    return mentions, None, WorkerStatus.SUCCEEDED


def _defer_checkpoint(reason: AudioRoutingDecision) -> str:
    return json.dumps({"reason": reason.value}, sort_keys=True)


# ---------------------------------------------------------------------------
# Phase 3 (Sarthak): micro-batch submission through Nipun's `/observations`
# endpoint, instead of bundling every observation into one terminal result.
# Reuses `_dispatch` (above) completely unchanged -- every profile already
# produces the identical `list[RawMention]` shape regardless of source
# modality, so only the *submission* strategy is new here, not the
# extraction logic itself. See docs/architecture/communication-processing.md
# and docs/architecture/phase-3-decisions.md's Sarthak section.
# ---------------------------------------------------------------------------

#: The transformation step name for a chunk-scoped local-audio ASR/
#: diarization publication -- distinct from `wav_metadata_extraction`
#: (the same path's own whole-file metadata mention, which stays
#: unscoped) and from `communication_extraction` (every other profile).
STEP_NAME_LOCAL_AUDIO_CHUNK = "local_audio_asr_diarization_extraction"

#: One safe, named transformation step per profile family -- recorded in
#: every batch's `TransformationProvenanceV1`, never raw source content.
_TRANSFORMATION_STEP_NAMES: dict[str, str] = {
    AUDIO_METADATA_V1.name: "wav_metadata_extraction",
    TRANSCRIPT_IMPORT_V1.name: "transcript_segment_import_validation",
    DIARIZATION_IMPORT_V1.name: "diarization_segment_import_validation",
    WHATSAPP_EXPORT_V1.name: "whatsapp_export_parsing",
    TELEGRAM_EXPORT_V1.name: "telegram_export_parsing",
    INSTAGRAM_EXPORT_V1.name: "instagram_export_parsing",
    GENERIC_SOCIAL_JSON_V1.name: "generic_social_json_parsing",
}


def _observations_for(
    job: WorkerJobV1, profile: ProcessorProfile, mentions: list[RawMention], now: datetime
) -> list[ObservationV1]:
    """Convert mentions to observations, one per mention, with stable IDs.

    Most mentions have a locator unique within the job, so this is
    ordinarily a 1:1 `mention_to_observation` mapping (`discriminator=""`,
    unchanged from before this helper existed). The one documented
    exception: `social/identifiers.py` intentionally reuses a chat
    message's own locator for every `phone_number`/`email_address`/
    `username_or_handle`/`url` mention it extracts from that message's
    text -- the message *is* the accurate source location; there is
    nothing more precise to point at. When more than one such mention
    shares `(observation_type, locator)`, later occurrences get a
    positional `discriminator` so they still get distinct, stable
    `observation_id`s instead of colliding.
    """
    seen: dict[tuple[str, str], int] = {}
    observations: list[ObservationV1] = []
    for mention in mentions:
        key = (mention.observation_type, mention.locator.model_dump_json())
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        observations.append(
            mention_to_observation(
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                profile=profile,
                mention=mention,
                created_at=now,
                discriminator=str(ordinal) if ordinal else "",
            )
        )
    return observations


def run_communication_job_with_batches(
    *,
    client: WorkerApiClient,
    job: WorkerJobV1,
    claim_token: str,
    input_payload: InputPayload,
    clock: Clock = _default_clock,
    audio_profile: AudioProcessingProfile | None = None,
    asr_adapter: AsrAdapter | None = None,
    diarization_adapter: DiarizationAdapter | None = None,
) -> WorkerResultV1:
    """Process one communication-processing job, submitting bounded micro-batches.

    Reuses `_get_profile`/`_validate_source_type`/`_dispatch` (this
    module's existing, unchanged extraction logic) to build the full list
    of mentions first, then submits them through `client.submit_batch` in
    bounded chunks (`Settings.communication_batch_size`) with real
    transformation provenance and monotonically-increasing progress, before
    submitting exactly one terminal `WorkerResultV1` with `observations=[]`
    -- the observations were already delivered. A `DEFERRED` outcome (raw
    audio with no real ASR/diarization capability available) submits zero
    batches, exactly as before this phase: there is nothing real to
    report. Never raises `ProcessingError` -- caught here and turned into a
    terminal `FAILED` result, exactly like `process_job`'s own contract.
    """
    settings = get_settings()
    now = clock()

    #: Set only when the local-audio path registers a real coordinator
    #: manifest (see P5-INTEG-COMMUNICATION-001) -- `None` for every other
    #: profile, which never had a manifest and is unaffected below.
    manifest: ChunkManifest | None = None
    try:
        profile = _get_profile(job.processor_name)
        _validate_source_type(profile, job.source_type)
        if (
            profile.name == AUDIO_METADATA_V1.name
            and isinstance(input_payload, AudioMetadataInput)
            and audio_profile is not None
            and asr_adapter is not None
            and diarization_adapter is not None
        ):
            duration_ms = round(extract_wav_metadata(input_payload.data).duration_seconds * 1000)
            candidate_manifest = plan_audio_manifest(
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                job_id=job.job_id,
                input_object_uri=job.input_object_uri,
                source_type=job.source_type.value,
                processor_name=job.processor_name,
                processor_version=job.processor_version,
                duration_ms=duration_ms,
                profile=audio_profile,
                created_at=now,
            )
            # Coordinator-owned persistence, not a worker-local plan the
            # coordinator never saw -- see `client.create_media_manifest`.
            manifest = client.create_media_manifest(
                job_id=job.job_id, claim_token=claim_token, manifest=candidate_manifest
            )
            mentions, checkpoint, status = _handle_local_audio(
                job=job,
                input_payload=input_payload,
                profile=audio_profile,
                asr_adapter=asr_adapter,
                diarization_adapter=diarization_adapter,
                manifest=manifest,
                created_at=now,
            )
        else:
            mentions, checkpoint, status = _dispatch(profile, input_payload)
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
            completed_at=now,
        )
    except WorkerApiError as exc:
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.FAILED,
            observations=[],
            derived_artifacts=[],
            checkpoint=None,
            error=WorkerError(
                code="media_manifest_registration_failed", message=str(exc), retryable=True
            ),
            completed_at=now,
        )

    if not mentions:
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=status,
            observations=[],
            derived_artifacts=[],
            checkpoint=checkpoint,
            error=None,
            completed_at=now,
        )

    step_name = _TRANSFORMATION_STEP_NAMES.get(profile.name, "communication_extraction")
    config_hash = profile_config_hash(profile)
    batch_size = settings.communication_batch_size
    total = len(mentions)
    renew_since_last = 0
    batch_sequence = 0
    units_completed = 0

    # A mention carrying a `chunk_id` attribute (only ASR/diarization
    # mentions from the local-audio path) must be published chunk-scoped,
    # combined per chunk (a coordinator-persisted chunk accepts exactly
    # one publication -- see `EvidenceLifecycleService.
    # _validate_media_publication`'s "already completed" check); every
    # other mention (including this same path's own whole-file metadata
    # mention, and every mention from every other profile) keeps using
    # the pre-existing unscoped micro-batch loop below, unchanged.
    chunk_groups: dict[str, list[RawMention]] = {}
    unscoped_mentions: list[RawMention] = []
    if manifest is not None:
        for mention in mentions:
            chunk_id = mention.attributes.get("chunk_id")
            if isinstance(chunk_id, str):
                chunk_groups.setdefault(chunk_id, []).append(mention)
            else:
                unscoped_mentions.append(mention)
    else:
        unscoped_mentions = mentions

    if chunk_groups:
        assert manifest is not None
        chunk_index_by_id = {
            str(chunk_identity(manifest, spec.index)): spec.index for spec in manifest.chunks
        }
        for chunk_id_str in sorted(chunk_groups, key=lambda cid: chunk_index_by_id.get(cid, -1)):
            chunk_index = chunk_index_by_id.get(chunk_id_str)
            if chunk_index is None:
                # Never guess which chunk an unrecognized id meant.
                raise WorkerApiError(
                    f"mention references chunk {chunk_id_str}, which is not part of "
                    f"manifest {manifest.manifest_id}"
                )
            group = chunk_groups[chunk_id_str]
            observations = _observations_for(job, profile, group, now)
            # Must match what `build_batch_submission` derives internally
            # from the same `(job_id, batch_sequence)` pair below -- a
            # transformation's `batch_id` must equal its submission's own.
            batch_id = deterministic_batch_id(job_id=job.job_id, batch_sequence=batch_sequence)
            units_completed += len(group)

            chunk_transformation: TransformationProvenanceV1 = build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=0,
                step_name=STEP_NAME_LOCAL_AUDIO_CHUNK,
                step_version=profile.version,
                config_hash=config_hash,
                started_at=now,
                completed_at=now,
                output_observation_ids=[o.observation_id for o in observations],
                safe_metadata={
                    "batch_sequence": batch_sequence,
                    "item_count": len(group),
                    "chunk_index": chunk_index,
                },
            )
            chunk_progress = build_progress(
                stage="submitting",
                units_total=total,
                units_completed=units_completed,
                observations_emitted=len(observations),
                batch_sequence=batch_sequence,
                occurred_at=now,
                message_code="CHUNK_BATCH_SUBMITTED",
            )
            chunk_submission = build_batch_submission(
                job=job,
                batch_sequence=batch_sequence,
                submitted_at=now,
                observations=observations,
                transformations=[chunk_transformation],
                progress=chunk_progress,
            )
            publication = MediaChunkPublication(
                manifest_id=manifest.manifest_id,
                manifest_hash=manifest.manifest_hash,
                chunk_id=chunk_identity(manifest, chunk_index),
                chunk_index=chunk_index,
                batch=chunk_submission,
                checkpoint_id=deterministic_uuid(
                    "phase5_media_checkpoint",
                    str(manifest.manifest_id),
                    str(chunk_identity(manifest, chunk_index)),
                ),
                completed_at=now,
            )
            client.publish_media_chunk(
                job_id=job.job_id, claim_token=claim_token, publication=publication
            )
            batch_sequence += 1
            renew_since_last += 1
            if renew_since_last >= 5:
                with contextlib.suppress(WorkerApiError):
                    client.renew_lease(job.job_id, claim_token=claim_token)
                renew_since_last = 0

    for start in range(0, len(unscoped_mentions), batch_size):
        chunk = unscoped_mentions[start : start + batch_size]
        observations = _observations_for(job, profile, chunk, now)
        batch_id = deterministic_batch_id(job_id=job.job_id, batch_sequence=batch_sequence)
        units_completed += len(chunk)

        transformation: TransformationProvenanceV1 = build_transformation(
            job=job,
            batch_id=batch_id,
            ordinal=0,
            step_name=step_name,
            step_version=profile.version,
            config_hash=config_hash,
            started_at=now,
            completed_at=now,
            output_observation_ids=[o.observation_id for o in observations],
            safe_metadata={"batch_sequence": batch_sequence, "item_count": len(chunk)},
        )
        progress = build_progress(
            stage="submitting",
            units_total=total,
            units_completed=units_completed,
            observations_emitted=len(observations),
            batch_sequence=batch_sequence,
            occurred_at=now,
            message_code="BATCH_SUBMITTED",
        )
        submission = build_batch_submission(
            job=job,
            batch_sequence=batch_sequence,
            submitted_at=now,
            observations=observations,
            transformations=[transformation],
            progress=progress,
        )
        client.submit_batch(job_id=job.job_id, claim_token=claim_token, submission=submission)
        batch_sequence += 1

        renew_since_last += 1
        if renew_since_last >= 5:
            # Best-effort heartbeat only; a failure here is not fatal to the job.
            with contextlib.suppress(WorkerApiError):
                client.renew_lease(job.job_id, claim_token=claim_token)
            renew_since_last = 0

    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=status,
        observations=[],
        derived_artifacts=[],
        checkpoint=checkpoint,
        error=None,
        completed_at=now,
    )


#: Every profile this worker's `process_job` dispatch table supports,
#: matching this module's own `_PROFILES` exactly (name, version). Resolved
#: (Phase 2 routing fix, `evidence_lifecycle/routing.py`): all seven are now
#: reachable through a real evidence upload -- `audio_metadata_v1` (from
#: `SourceType.AUDIO`) and `generic_social_json_v1` (from `SourceType.CHAT`)
#: since Phase 1, and `transcript_import_v1`/`diarization_import_v1`/
#: `whatsapp_export_v1`/`telegram_export_v1`/`instagram_export_v1` (from
#: their own new, disjoint `SourceType`s) since the routing fix. See
#: `docs/architecture/evidence-lifecycle.md`'s routing table and
#: `docs/architecture/phase-2-decisions.md`.
SUPPORTED_PROCESSORS: tuple[tuple[str, str], ...] = tuple(
    (profile.name, profile.version) for profile in _PROFILES.values()
)

#: A safe, non-secret checkpoint recorded on a `DEFERRED` result when the
#: claimed job's evidence couldn't be resolved because the input-access
#: endpoint isn't reachable -- mirrors `structured_processing.worker`'s
#: identical addition: an honest, expected "not yet possible" outcome,
#: never a fabricated failure or a crash.
CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE = "input_resolution_unavailable"


@dataclass(frozen=True)
class RunOnceOutcome:
    """What one `run_once` call actually did -- for the CLI's exit behavior and for tests."""

    claimed: bool
    job_id: UUID | None
    result_status: str | None
    deferred_reason: str | None = None


def run_once(
    *,
    client: WorkerApiClient,
    input_resolver: WorkerInputResolver,
    clock: Clock = _default_clock,
    processors: Sequence[tuple[str, str]] = SUPPORTED_PROCESSORS,
    audio_profile: AudioProcessingProfile | None = None,
    asr_adapter: AsrAdapter | None = None,
    diarization_adapter: DiarizationAdapter | None = None,
) -> RunOnceOutcome:
    """Claim at most one job, process it, submit its result, and return what happened.

    Tries each supported `(processor_name, processor_version)` in turn
    until one yields a job, or returns `claimed=False` once all are
    exhausted -- a normal, successful "no work" outcome, never an error.
    Never submits `queued`/`running` as a result: `process_job` only ever
    returns a terminal `WorkerResultV1`, and the input-resolution-gap and
    integrity-mismatch paths below both submit a terminal result too,
    never fabricating `SUCCEEDED`.
    """
    run_id = str(uuid4())
    structlog.contextvars.bind_contextvars(run_id=run_id)
    try:
        logger.info("worker.run_once.started")
        claim_result = None
        for processor_name, processor_version in processors:
            claim_result = client.claim(
                processor_name=processor_name, processor_version=processor_version
            )
            if claim_result.job is not None:
                break

        if claim_result is None or claim_result.job is None:
            logger.info("worker.run_once.no_job_available")
            return RunOnceOutcome(claimed=False, job_id=None, result_status=None)

        job = claim_result.job
        claim_token = claim_result.claim_token
        if claim_token is None:  # pragma: no cover - defensive: API always pairs job+token
            raise WorkerApiError("internal worker API returned a job without a claim token")

        structlog.contextvars.bind_contextvars(job_id=str(job.job_id))
        logger.info("worker.run_once.claimed", processor_name=job.processor_name)

        try:
            resolved = input_resolver.resolve(job, claim_token=claim_token)
        except InputResolutionUnavailableError as exc:
            logger.warning("worker.run_once.input_unavailable")
            deferred = _deferred_result_for_missing_input(job, clock())
            client.submit_result(job_id=job.job_id, claim_token=claim_token, result=deferred)
            return RunOnceOutcome(
                claimed=True,
                job_id=job.job_id,
                result_status=deferred.status.value,
                deferred_reason=str(exc),
            )

        if resolved.expected_sha256 is not None:
            actual_sha256 = hashlib.sha256(resolved.data).hexdigest()
            if actual_sha256 != resolved.expected_sha256:
                logger.error("worker.run_once.integrity_mismatch")
                mismatch = _failed_result_for_integrity_mismatch(job, clock())
                ack = client.submit_result(
                    job_id=job.job_id, claim_token=claim_token, result=mismatch
                )
                return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)

        try:
            input_payload = _build_input_payload(job, resolved)
        except ProcessingError as exc:
            logger.warning("worker.run_once.input_payload_invalid", error_code=exc.code)
            failed = _failed_result_from_processing_error(job, exc, clock())
            ack = client.submit_result(job_id=job.job_id, claim_token=claim_token, result=failed)
            return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)

        result = run_communication_job_with_batches(
            client=client,
            job=job,
            claim_token=claim_token,
            input_payload=input_payload,
            clock=clock,
            audio_profile=audio_profile,
            asr_adapter=asr_adapter,
            diarization_adapter=diarization_adapter,
        )
        ack = client.submit_result(job_id=job.job_id, claim_token=claim_token, result=result)
        logger.info(
            "worker.run_once.submitted",
            status=ack.status,
            observation_count=ack.observation_count,
        )
        return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "job_id")


def _deferred_result_for_missing_input(job: WorkerJobV1, completed_at: datetime) -> WorkerResultV1:
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.DEFERRED,
        observations=[],
        derived_artifacts=[],
        checkpoint=CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE,
        error=None,
        completed_at=completed_at,
    )


def _failed_result_for_integrity_mismatch(
    job: WorkerJobV1, completed_at: datetime
) -> WorkerResultV1:
    """A genuine data-integrity problem (not "not yet possible") -- `FAILED`, not `DEFERRED`.

    `retryable=True`: a fresh claim/re-stream could plausibly succeed if
    the mismatch was caused by a one-off transport issue rather than a
    persistently corrupt stored object.
    """
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=WorkerError(
            code="evidence_integrity_mismatch",
            message="resolved evidence bytes do not match the expected SHA-256",
            retryable=True,
        ),
        completed_at=completed_at,
    )


def _failed_result_from_processing_error(
    job: WorkerJobV1, exc: ProcessingError, completed_at: datetime
) -> WorkerResultV1:
    """Mirrors `process_job`'s own `except ProcessingError` handling, for errors raised
    one step earlier -- while building the typed `InputPayload` itself, before
    `process_job` (which never sees raw bytes) is ever called."""
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


def _build_input_payload(job: WorkerJobV1, resolved: ResolvedInput) -> InputPayload:
    """Build the typed `InputPayload` `process_job` expects, from a claimed job's
    resolved evidence bytes.

    The dispatch key is `job.processor_name` alone -- the same
    already-authenticated, server-assigned value `process_job` itself
    dispatches on, never a client-controlled field, filename, or sniffed
    content. Raises `unsupported_processor` for anything not in
    `_PROFILES` (defensive: `run_once` only ever claims a profile from
    `SUPPORTED_PROCESSORS`, which is derived from `_PROFILES`).
    """
    processor_name = job.processor_name
    if processor_name == AUDIO_METADATA_V1.name:
        return AudioMetadataInput(filename=resolved.original_filename, data=resolved.data)
    if processor_name == TRANSCRIPT_IMPORT_V1.name:
        return parse_transcript_import_payload(resolved.data)
    if processor_name == DIARIZATION_IMPORT_V1.name:
        return parse_diarization_import_payload(resolved.data)
    if processor_name in _PLATFORM_TO_PROFILE_NAME.values():
        platform = next(
            candidate
            for candidate, profile_name in _PLATFORM_TO_PROFILE_NAME.items()
            if profile_name == processor_name
        )
        return SocialExportInput(platform=platform, data=resolved.data)  # type: ignore[arg-type]
    raise ProcessingError(
        ErrorCode.UNSUPPORTED_PROCESSOR,
        f"'{processor_name}' is not a processor this worker's CLI runner supports",
    )


def _configure_logging() -> None:
    """Structured JSON logging to stdout, mirroring `app.main`'s configuration.

    Deliberately duplicated rather than imported from `app.main`: importing
    it would construct the full FastAPI application (every router, every
    other module's module-level dependency wiring) just to log one line
    from a one-shot CLI script.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _build_client(settings: Settings) -> WorkerApiClient:
    token = settings.worker_token
    if token is None:
        raise WorkerAuthenticationError(
            "WORKER_TOKEN is not configured for this worker process; see "
            "docs/architecture/worker-identity-and-security.md"
        )
    return WorkerApiClient(
        base_url=settings.worker_api_base_url, worker_token=token.get_secret_value()
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: `uv run python -m app.modules.communication_processing.worker --once`."""
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.communication_processing.worker",
        description=(
            "Claim and process at most one compatible communication-processing job, then "
            "exit. No daemon or polling mode exists in this phase."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Run exactly one claim-process-submit cycle, then exit.",
    )
    parser.add_argument(
        "--profile",
        choices=("rapid", "deep"),
        default=None,
        help="Override COMMUNICATION_AUDIO_PROFILE for raw-audio jobs only.",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()
    selected_profile = args.profile or settings.communication_audio_profile
    audio_profile = RAPID_AUDIO_PROFILE if selected_profile == "rapid" else DEEP_AUDIO_PROFILE
    asr_adapter = LocalCommandAsrAdapter(
        command=settings.communication_asr_command,
        model_path=settings.communication_asr_model_path,
        language=settings.communication_asr_language,
        timeout_seconds=settings.communication_asr_timeout_seconds,
    )
    diarization_adapter: DiarizationAdapter = LocalCommandDiarizationAdapter(
        command=settings.communication_diarization_command,
        model_path=settings.communication_diarization_model_path,
        timeout_seconds=settings.communication_diarization_timeout_seconds,
    )
    try:
        client = _build_client(settings)
    except WorkerAuthenticationError as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1

    try:
        outcome = run_once(
            client=client,
            input_resolver=LiveInputResolver(client),
            audio_profile=audio_profile,
            asr_adapter=asr_adapter,
            diarization_adapter=diarization_adapter,
        )
    except (WorkerAuthenticationError, WorkerApiError, InputResolutionUnavailableError) as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1
    finally:
        client.close()

    logger.info(
        "worker.cli.done",
        job_id=str(outcome.job_id) if outcome.job_id else None,
        status=outcome.result_status or "no_job_available",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "AUDIO_METADATA_V1",
    "CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE",
    "DIARIZATION_IMPORT_V1",
    "GENERIC_SOCIAL_JSON_V1",
    "INSTAGRAM_EXPORT_V1",
    "SUPPORTED_PROCESSORS",
    "TELEGRAM_EXPORT_V1",
    "TRANSCRIPT_IMPORT_V1",
    "WHATSAPP_EXPORT_V1",
    "RunOnceOutcome",
    "main",
    "process_job",
    "run_once",
]
