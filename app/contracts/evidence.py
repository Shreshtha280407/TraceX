"""EvidenceRecordV1: the contract for a single ingested piece of evidence.

This is a contract only — Phase 1 does not implement upload storage,
hashing, or MinIO writes. `object_uri` and `sha256` describe where the
bytes eventually live and what integrity value they must match; producing
those values is later-phase work.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.contracts.common import ContractVersion, TraceXModel


class SourceType(StrEnum):
    """Evidence modality, per the TraceX problem scope.

    `OTHER` is the deliberate escape hatch: it lets evidence of an
    unanticipated modality be ingested without widening this enum, keeping
    the contract stable while later phases add modality-specific handling.

    `STRUCTURED_TABULAR`/`STRUCTURED_JSON` (Phase 2.3) are an additive,
    backward-compatible extension: every previously-valid `SourceType`
    value remains valid and unchanged. They give general CSV/XLSX/JSON
    evidence that isn't specifically CDR or financial shaped an explicit,
    server-routable source type of its own, reaching
    `structured_processing`'s existing `generic_tabular_v1`/
    `generic_json_v1` fallback profiles — see
    `docs/architecture/evidence-lifecycle.md`.
    """

    DOCUMENT = "document"
    CDR = "cdr"
    FINANCIAL = "financial"
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    CHAT = "chat"
    STRUCTURED_TABULAR = "structured_tabular"
    STRUCTURED_JSON = "structured_json"
    OTHER = "other"


class EvidenceClassification(StrEnum):
    """Sensitivity label. Phase 1 defines the label only; RBAC/ABAC
    enforcement on it is later-phase work."""

    UNCLASSIFIED = "unclassified"
    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


class EvidenceProcessingStatus(StrEnum):
    """Lifecycle of an evidence record through ingestion and worker processing."""

    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class EvidenceRecordV1(TraceXModel):
    """A single piece of ingested evidence and its processing state.

    `uploaded_at` fulfills the common "created_at" contract rule for this
    record: the record's existence and its upload are the same event.
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    evidence_id: UUID
    case_id: UUID
    source_type: SourceType
    original_filename: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    object_uri: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification: EvidenceClassification
    uploaded_by: str = Field(min_length=1)
    uploaded_at: AwareDatetime
    parser_profile: str | None = None
    processing_status: EvidenceProcessingStatus
