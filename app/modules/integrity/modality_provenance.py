"""Safe, versioned provenance commitments for visual and communication observations.

These projections are intentionally separate from the structured-document
projection.  They are made only after an observation is accepted by the
evidence lifecycle and retain source *structure*, never source content,
identifiers, media bytes, pixels, or local labels.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.evidence import SourceType
from app.contracts.observation import ObservationV1
from app.core.canonical import canonical_sha256
from app.modules.evidence_lifecycle.media_orchestration import MediaChunkPublication
from app.modules.integrity.models import (
    IntegrityEventKind,
    IntegrityEventSubmission,
    IntegrityModel,
)

VISUAL_OBSERVATION_PROVENANCE_SCHEMA_VERSION = "visual_provenance.v1"
COMMUNICATION_OBSERVATION_PROVENANCE_SCHEMA_VERSION = "communication_provenance.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CATEGORY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class VisualSourceFamily(StrEnum):
    VISUAL = "visual"


class CommunicationSourceFamily(StrEnum):
    AUDIO = "audio"
    SOCIAL = "social"


class _ModalityProjection(IntegrityModel):
    """Shared safe fields; subclasses retain distinct semantics and leaves."""

    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    observation_type: str = Field(min_length=1, max_length=128)
    evidence_sha256: str
    extractor_name: str = Field(min_length=1, max_length=128)
    extractor_version: str = Field(min_length=1, max_length=128)
    extractor_config_hash: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    validation_outcome: str = Field(min_length=1, max_length=32)
    correlation_ready: bool
    safe_source_locator_commitment_sha256: str

    @model_validator(mode="after")
    def _validate_commitments(self) -> _ModalityProjection:
        for field_name in self.__class__.model_fields:
            if field_name.endswith("_sha256"):
                candidate = getattr(self, field_name)
                if candidate is not None and not _SHA256.fullmatch(candidate):
                    raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
        return self

    def canonical_metadata(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def canonical_payload_sha256(self) -> str:
        return canonical_sha256(self.canonical_metadata())


class VisualObservationIntegrityProvenanceV1(_ModalityProjection):
    """Safe commitment for a persisted, validated video/image/OCR/track observation."""

    schema_version: Literal["visual_provenance.v1"] = "visual_provenance.v1"
    source_family: Literal[VisualSourceFamily.VISUAL] = VisualSourceFamily.VISUAL
    persisted_manifest_id: UUID
    persisted_chunk_id: UUID
    chunk_boundary_version_commitment_sha256: str
    source_relative_interval_commitment_sha256: str | None = None
    frame_mapping_commitment_sha256: str | None = None
    normalized_geometry_commitment_sha256: str | None = None
    detection_category: str | None = Field(default=None, max_length=64)
    local_track_commitment_sha256: str | None = None
    local_track_lifecycle_condition: str | None = Field(default=None, max_length=64)
    text_content_commitment_sha256: str | None = None

    @property
    def idempotency_key(self) -> str:
        return f"visual-provenance:{self.case_id}:{self.observation_id}:{self.schema_version}"

    def to_integrity_submission(self, *, source_created_at: datetime) -> IntegrityEventSubmission:
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.OBSERVATION_PUBLISHED,
            subject_type="visual_observation_provenance",
            subject_id=str(self.observation_id),
            canonical_metadata=self.canonical_metadata(),
            payload_schema_version=self.schema_version,
            source_created_at=source_created_at,
            idempotency_key=self.idempotency_key,
        )


class CommunicationObservationIntegrityProvenanceV1(_ModalityProjection):
    """Safe commitment for a validated audio, diarization, or social observation."""

    schema_version: Literal["communication_provenance.v1"] = "communication_provenance.v1"
    source_family: CommunicationSourceFamily
    persisted_manifest_id: UUID | None = None
    persisted_chunk_id: UUID | None = None
    chunk_boundary_version_commitment_sha256: str | None = None
    source_relative_interval_commitment_sha256: str | None = None
    audio_text_commitment_sha256: str | None = None
    local_speaker_label_commitment_sha256: str | None = None
    language_or_script: str | None = Field(default=None, max_length=32)
    platform_namespace: str | None = Field(default=None, max_length=64)
    message_identifier_commitment_sha256: str | None = None
    participant_commitment_sha256: str | None = None
    attachment_content_commitment_sha256: str | None = None
    communication_category: str | None = Field(default=None, max_length=64)

    @property
    def idempotency_key(self) -> str:
        return (
            f"communication-provenance:{self.case_id}:{self.observation_id}:{self.schema_version}"
        )

    def to_integrity_submission(self, *, source_created_at: datetime) -> IntegrityEventSubmission:
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.OBSERVATION_PUBLISHED,
            subject_type="communication_observation_provenance",
            subject_id=str(self.observation_id),
            canonical_metadata=self.canonical_metadata(),
            payload_schema_version=self.schema_version,
            source_created_at=source_created_at,
            idempotency_key=self.idempotency_key,
        )


def build_visual_observation_provenance(
    *,
    observation: ObservationV1,
    evidence_sha256: str,
    source_type: SourceType,
    publication: MediaChunkPublication | None,
) -> VisualObservationIntegrityProvenanceV1 | None:
    """Project only an accepted visual observation in a persisted media chunk.

    ``publication`` is supplied by the lifecycle only after it has validated
    the persisted manifest/chunk relationship.  A missing scope is therefore
    an ineligible observation, never an opportunity to guess one.
    """
    if source_type not in {SourceType.VIDEO, SourceType.IMAGE} or publication is None:
        return None
    validation = _accepted_validation(observation.attributes.get("visual_signal_validation"))
    if validation is None:
        return None
    locator = observation.source_locator.model_dump(mode="json", exclude_none=True)
    attributes = observation.attributes
    lifecycle = _safe_lifecycle(attributes.get("track_lifecycle_conditions"))
    track_value = attributes.get("track_id", attributes.get("local_track_id"))
    text_values = [entity.text for entity in observation.extracted_entities]
    return VisualObservationIntegrityProvenanceV1.model_validate(
        {
            "case_id": observation.case_id,
            "evidence_id": observation.evidence_id,
            "observation_id": observation.observation_id,
            "observation_type": observation.observation_type,
            "evidence_sha256": evidence_sha256,
            "extractor_name": observation.extractor.name,
            "extractor_version": observation.extractor.version,
            "extractor_config_hash": observation.extractor.config_hash,
            "model_version": observation.extractor.model_version,
            "validation_outcome": validation["outcome"],
            "correlation_ready": validation["correlation_ready"],
            "safe_source_locator_commitment_sha256": _commit(locator),
            "persisted_manifest_id": publication.manifest_id,
            "persisted_chunk_id": publication.chunk_id,
            "chunk_boundary_version_commitment_sha256": _commit(
                {"manifest_hash": publication.manifest_hash, "chunk_index": publication.chunk_index}
            ),
            "source_relative_interval_commitment_sha256": _interval_commitment(locator),
            "frame_mapping_commitment_sha256": _optional_commit(
                {"frame_number": observation.source_locator.frame_number}
                if observation.source_locator.frame_number is not None
                else None
            ),
            "normalized_geometry_commitment_sha256": _optional_commit(
                observation.source_locator.bbox_xyxy_normalized
            ),
            "detection_category": _safe_category(attributes.get("detected_label")),
            "local_track_commitment_sha256": _optional_commit(track_value),
            "local_track_lifecycle_condition": lifecycle,
            "text_content_commitment_sha256": _optional_commit(text_values)
            if text_values
            else None,
        }
    )


def build_communication_observation_provenance(
    *,
    observation: ObservationV1,
    evidence_sha256: str,
    source_type: SourceType,
    publication: MediaChunkPublication | None,
) -> CommunicationObservationIntegrityProvenanceV1 | None:
    """Project accepted audio/social observations without granting new eligibility."""
    source_family = _communication_family(source_type)
    if source_family is None:
        return None
    validation = _accepted_validation(observation.attributes.get("communication_signal_validation"))
    if validation is None:
        return None
    if source_type is SourceType.AUDIO and publication is None:
        # Raw-audio ASR/diarization provenance requires a coordinator-persisted
        # chunk. Imported transcript/diarization observations are not media
        # publications, so they retain no invented manifest/chunk scope.
        return None
    locator = observation.source_locator.model_dump(mode="json", exclude_none=True)
    attributes = observation.attributes
    entity_values = [entity.text for entity in observation.extracted_entities]
    message_id = attributes.get("message_id", observation.source_locator.message_id)
    participant_values = {
        "sender": attributes.get("sender"),
        "participants": attributes.get("participants"),
        "identifier": attributes.get("normalized_identifier"),
    }
    return CommunicationObservationIntegrityProvenanceV1.model_validate(
        {
            "case_id": observation.case_id,
            "evidence_id": observation.evidence_id,
            "observation_id": observation.observation_id,
            "source_family": source_family,
            "observation_type": observation.observation_type,
            "evidence_sha256": evidence_sha256,
            "extractor_name": observation.extractor.name,
            "extractor_version": observation.extractor.version,
            "extractor_config_hash": observation.extractor.config_hash,
            "model_version": observation.extractor.model_version,
            "validation_outcome": validation["outcome"],
            "correlation_ready": validation["correlation_ready"],
            "safe_source_locator_commitment_sha256": _commit(locator),
            "persisted_manifest_id": publication.manifest_id if publication else None,
            "persisted_chunk_id": publication.chunk_id if publication else None,
            "chunk_boundary_version_commitment_sha256": _commit(
                {"manifest_hash": publication.manifest_hash, "chunk_index": publication.chunk_index}
            )
            if publication
            else None,
            "source_relative_interval_commitment_sha256": _interval_commitment(locator),
            "audio_text_commitment_sha256": _optional_commit(
                attributes.get("transcript_text_sha256") or attributes.get("message_text_sha256")
            ),
            "local_speaker_label_commitment_sha256": _optional_commit(entity_values)
            if observation.observation_type == "diarization_turn" and entity_values
            else None,
            "language_or_script": _safe_language(attributes.get("language_hint")),
            "platform_namespace": _safe_category(
                attributes.get("platform_namespace", attributes.get("platform"))
            ),
            "message_identifier_commitment_sha256": _optional_commit(message_id),
            "participant_commitment_sha256": _optional_commit(participant_values)
            if any(value is not None for value in participant_values.values())
            else None,
            "attachment_content_commitment_sha256": _optional_commit(
                attributes.get("attachment_sha256") or attributes.get("attachment_content_sha256")
            ),
            "communication_category": _safe_category(
                attributes.get("match_kind", attributes.get("communication_type"))
            ),
        }
    )


def _accepted_validation(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or value.get("outcome") != "accepted":
        return None
    if value.get("correlation_ready") is not True:
        return None
    return {"outcome": "accepted", "correlation_ready": True}


def _communication_family(source_type: SourceType) -> CommunicationSourceFamily | None:
    if source_type in {SourceType.AUDIO, SourceType.AUDIO_TRANSCRIPT, SourceType.AUDIO_DIARIZATION}:
        return CommunicationSourceFamily.AUDIO
    if source_type in {
        SourceType.CHAT,
        SourceType.WHATSAPP_CHAT,
        SourceType.TELEGRAM_CHAT,
        SourceType.INSTAGRAM_CHAT,
    }:
        return CommunicationSourceFamily.SOCIAL
    return None


def _commit(value: object) -> str:
    return canonical_sha256({"committed_value": value})


def _optional_commit(value: object) -> str | None:
    return _commit(value) if value is not None else None


def _interval_commitment(locator: dict[str, Any]) -> str | None:
    interval = {
        key: locator[key]
        for key in ("time_start_ms", "time_end_ms")
        if key in locator and locator[key] is not None
    }
    return _commit(interval) if interval else None


def _safe_category(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _SAFE_CATEGORY.fullmatch(candidate) else None


def _safe_lifecycle(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    candidates = sorted(
        item.strip().lower()
        for item in value
        if isinstance(item, str) and _SAFE_CATEGORY.fullmatch(item.strip().lower())
    )
    return "+".join(candidates) if candidates and len("+".join(candidates)) <= 64 else None


def _safe_language(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if re.fullmatch(r"[a-z0-9_-]{1,32}", candidate) else None


ModalityObservationIntegrityProvenanceV1 = (
    VisualObservationIntegrityProvenanceV1 | CommunicationObservationIntegrityProvenanceV1
)

__all__ = [
    "COMMUNICATION_OBSERVATION_PROVENANCE_SCHEMA_VERSION",
    "CommunicationObservationIntegrityProvenanceV1",
    "CommunicationSourceFamily",
    "ModalityObservationIntegrityProvenanceV1",
    "VISUAL_OBSERVATION_PROVENANCE_SCHEMA_VERSION",
    "VisualObservationIntegrityProvenanceV1",
    "VisualSourceFamily",
    "build_communication_observation_provenance",
    "build_visual_observation_provenance",
]
