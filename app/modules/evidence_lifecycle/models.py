"""Typed internal persistence records for the evidence-lifecycle module.

Mirrors `app.modules.access_control.models`'s pattern: these are internal,
not `app/contracts/` contracts. `EvidenceRecord`/`WorkerJobRecord` are this
module's PostgreSQL row shapes; `to_contract()` on each produces the frozen
`EvidenceRecordV1`/`WorkerJobV1` a worker actually needs, so lifecycle-only
bookkeeping columns (idempotency keys, dispatch timestamps, last-error
detail) never leak into the contract surface.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
    SourceType,
)
from app.contracts.worker import WorkerJobV1, WorkerStatus


class EvidenceLifecycleModel(BaseModel):
    """Base class for internal evidence-lifecycle models: immutable, no stray fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRecord(EvidenceLifecycleModel):
    """A full `evidence_records` row."""

    evidence_id: UUID
    case_id: UUID
    source_type: SourceType
    original_filename: str
    content_type: str
    object_uri: str
    sha256: str
    classification: EvidenceClassification
    uploaded_by: UUID
    uploaded_at: datetime
    parser_profile: str | None
    processing_status: EvidenceProcessingStatus
    upload_idempotency_key: str | None
    created_at: datetime
    updated_at: datetime

    def to_contract(self) -> EvidenceRecordV1:
        return EvidenceRecordV1(
            evidence_id=self.evidence_id,
            case_id=self.case_id,
            source_type=self.source_type,
            original_filename=self.original_filename,
            content_type=self.content_type,
            object_uri=self.object_uri,
            sha256=self.sha256,
            classification=self.classification,
            uploaded_by=str(self.uploaded_by),
            uploaded_at=self.uploaded_at,
            parser_profile=self.parser_profile,
            processing_status=self.processing_status,
        )


class WorkerJobRecord(EvidenceLifecycleModel):
    """A full `worker_jobs` row.

    `status` is always `WorkerStatus.QUEUED` in this phase: no consumer
    exists yet to transition it to `running`/`succeeded`/`failed` (see
    `docs/qa/known-limitations.md`). `dispatched_at` tracks whether the
    best-effort Redis publish succeeded, independent of `status` -- a job
    with `dispatched_at is None` is still durable (the row itself is the
    source of truth) and safely re-publishable by a future redrive.
    """

    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    source_type: SourceType
    processor_name: str
    processor_version: str
    attempt: int
    idempotency_key: str
    input_object_uri: str
    requested_at: datetime
    status: WorkerStatus
    queued_at: datetime | None
    dispatched_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    last_error_retryable: bool | None
    created_at: datetime
    updated_at: datetime

    def to_contract(self) -> WorkerJobV1:
        return WorkerJobV1(
            job_id=self.job_id,
            case_id=self.case_id,
            evidence_id=self.evidence_id,
            source_type=self.source_type,
            processor_name=self.processor_name,
            processor_version=self.processor_version,
            attempt=self.attempt,
            idempotency_key=self.idempotency_key,
            input_object_uri=self.input_object_uri,
            requested_at=self.requested_at,
        )
