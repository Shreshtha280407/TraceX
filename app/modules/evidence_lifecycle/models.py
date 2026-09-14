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
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
    SourceType,
)
from app.contracts.observation_batch import TransformationStatus
from app.contracts.worker import WorkerJobV1, WorkerResultV1, WorkerStatus


class WorkerAvailabilityStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    RECOVERING = "recovering"
    DEGRADED = "degraded"


#: Terminal `WorkerStatus` values -- a job is only ever transitioned to one
#: of these by a submitted `WorkerResultV1`; `queued`/`running` are never
#: valid submission outcomes. See `docs/architecture/worker-job-lifecycle.md`.
TERMINAL_WORKER_STATUSES = frozenset(
    {WorkerStatus.SUCCEEDED, WorkerStatus.FAILED, WorkerStatus.DEFERRED, WorkerStatus.CANCELLED}
)


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

    `status` starts at `WorkerStatus.QUEUED`; a claim transitions it to
    `running`, and a submitted `WorkerResultV1` transitions it to one of
    the terminal statuses (`TERMINAL_WORKER_STATUSES`) -- see
    `docs/architecture/worker-job-lifecycle.md`. `dispatched_at` tracks
    whether the best-effort Redis publish succeeded, independent of
    `status` -- a job with `dispatched_at is None` is still durable (the
    row itself is the source of truth) and safely re-publishable by a
    future redrive.

    `claimed_by` stores the claiming worker's self-declared `processor_name`
    only -- a non-secret identifier, kept unchanged from Phase 2.1 for
    observability. `claimed_by_worker_id` (Aditya's worker-identity
    hardening) is the *verified* identity: the `worker_id` of the
    authenticated `WorkerCredentialRecord` that actually performed the
    successful claim, set unconditionally on every claim and reclaim -- see
    `docs/architecture/worker-identity-and-security.md`. `claim_token_hash`
    is the SHA-256 hex digest of the one-time claim token handed to the
    worker; the raw token is never persisted.
    """

    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    source_type: SourceType
    processor_name: str
    processor_version: str
    attempt: int
    #: Bounds how many times this job may ever be claimed/reclaimed --
    #: mirrors `graph_projection_jobs.max_attempts`'s identical policy (see
    #: `app.modules.graph.outbox_repository`). Set once at job creation from
    #: `Settings.worker_job_max_attempts`; never changed afterward.
    max_attempts: int
    idempotency_key: str
    input_object_uri: str
    requested_at: datetime
    status: WorkerStatus
    queued_at: datetime | None
    dispatched_at: datetime | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    claimed_by: str | None
    claimed_by_worker_id: UUID | None
    claim_token_hash: str | None
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


class WorkerResultRecord(EvidenceLifecycleModel):
    """A full `worker_results` row -- one canonical `WorkerResultV1` submission.

    `canonical_payload` is the submitted result's own `model_dump(mode="json")`
    (so it can be reconstructed exactly via `to_contract()`); `payload_hash`
    is `app.core.canonical.canonical_sha256` of the same payload, the
    idempotency-comparison key for a resubmitted claim. `error_*` mirror
    `WorkerJobRecord.last_error_*`'s naming, but describe *this specific
    attempt's* outcome rather than the job's current denormalized state.
    """

    result_id: UUID
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    attempt: int
    status: WorkerStatus
    derived_artifacts: list[dict[str, JsonValue]]
    checkpoint: str | None
    error_code: str | None
    error_message: str | None
    error_retryable: bool | None
    canonical_payload: dict[str, JsonValue]
    payload_hash: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def to_contract(self) -> WorkerResultV1:
        return WorkerResultV1.model_validate(self.canonical_payload)


class ObservationRecord(EvidenceLifecycleModel):
    """A full `worker_observations` row -- one persisted `ObservationV1`.

    `canonical_payload` is the observation's own `model_dump(mode="json")`;
    reconstructing it as a contract is `ObservationV1.model_validate(record.canonical_payload)`.
    Immutable and insert-only once persisted -- no `updated_at`.

    Exactly one of `result_id` (a terminal `WorkerResultV1` submission, Phase
    2.1) / `observation_batch_id` (a partial micro-batch submission, Phase 3)
    is ever set -- never both, never neither. This is the "equivalent durable
    linkage" Phase 3 chose over a second, parallel observations table: both
    submission paths write into this same table, so every downstream reader
    (`count_observations_for_job`, `app.modules.graph.outbox_repository
    .get_observation`) needs no change to see observations from either path.
    See `docs/architecture/phase-3-decisions.md`.
    """

    observation_id: UUID
    result_id: UUID | None
    observation_batch_id: UUID | None
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_type: str
    canonical_payload: dict[str, JsonValue]
    created_at: datetime


class ObservationBatchRecord(EvidenceLifecycleModel):
    """A full `observation_batches` row -- one accepted partial-batch receipt.

    Immutable and insert-only: a batch is either accepted once (this row is
    created) or it isn't (nothing persists) -- there is no in-between status
    to transition later. `payload_hash` is `app.core.canonical.canonical_sha256`
    of the submitted `ObservationBatchSubmissionV1`, the same idempotency-
    comparison technique `WorkerResultRecord.payload_hash` already
    established for terminal results.
    """

    observation_batch_id: UUID
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    batch_id: str
    batch_sequence: int
    idempotency_key: str
    is_final_batch: bool
    observation_count: int
    payload_hash: str
    submitted_at: datetime
    created_at: datetime


class ObservationTransformationRecord(EvidenceLifecycleModel):
    """A full `observation_transformations` row -- one durable provenance step.

    `transformation_id` is worker-supplied (like `ObservationRecord
    .observation_id`), globally unique, and immutable once persisted --
    reconstructing it as a contract is
    `TransformationProvenanceV1.model_validate(record.canonical_payload)`.
    """

    transformation_id: UUID
    observation_batch_id: UUID
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    ordinal: int
    step_name: str
    status: TransformationStatus
    canonical_payload: dict[str, JsonValue]
    created_at: datetime


class WorkerProgressEventRecord(EvidenceLifecycleModel):
    """A full `worker_progress_events` row -- one durable, ordered progress report.

    `ordinal` is server-assigned (a `BIGSERIAL` column, globally monotonic --
    not per-job) purely to give a stable, race-free total order to "which
    event came after which"; it carries no other meaning. `attempt` is the
    claimed job's `WorkerJobRecord.attempt` at the moment this event was
    accepted, used to distinguish a genuine progress regression (rejected)
    from a fresh reclaim's legitimate reset (allowed) -- see
    `docs/architecture/phase-3-decisions.md`.
    """

    progress_event_id: UUID
    ordinal: int
    observation_batch_id: UUID
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    attempt: int
    stage: str
    units_total: int | None
    units_completed: int
    observations_emitted: int
    batch_sequence: int
    message_code: str | None
    occurred_at: datetime
    created_at: datetime


class MediaManifestRecord(EvidenceLifecycleModel):
    """Immutable coordinator-owned media work definition; never contains media bytes."""

    manifest_id: UUID
    case_id: UUID
    evidence_id: UUID
    job_id: UUID
    version: str
    manifest_hash: str
    processor_name: str
    processor_version: str
    configuration_hash: str
    canonical_payload: dict[str, JsonValue]
    created_at: datetime
    completed_at: datetime | None = None


class MediaChunkRecord(EvidenceLifecycleModel):
    """One immutable ordered chunk plus its accepted publication identity."""

    chunk_id: UUID
    manifest_id: UUID
    case_id: UUID
    evidence_id: UUID
    job_id: UUID
    chunk_index: int
    canonical_boundary: dict[str, JsonValue]
    status: str
    publication_hash: str | None
    observation_batch_id: UUID | None
    completed_at: datetime | None
    created_at: datetime


class MediaCheckpointRecord(EvidenceLifecycleModel):
    """A durable snapshot written atomically with a completed chunk publication."""

    checkpoint_id: UUID
    manifest_id: UUID
    case_id: UUID
    evidence_id: UUID
    job_id: UUID
    manifest_hash: str
    processor_version: str
    configuration_hash: str
    completed_chunk_ids: list[UUID]
    observation_batch_ids: list[UUID]
    artifact_ids: list[UUID]
    created_at: datetime
