"""API-facing response shapes for the evidence-lifecycle module.

Kept internal to this module, like `access_control.models`'s public-shape
section. Deliberately never includes `object_uri`, storage credentials, or
any presigned/permanent object-storage URL -- see "no unrestricted
raw-evidence-download APIs" in `docs/architecture/evidence-lifecycle.md`.
Upload itself has no request-body schema here: it is `multipart/form-data`,
parsed directly by FastAPI's `File`/`Form` parameters in `api.py`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.observation_batch import ObservationBatchProgressV1
from app.contracts.worker import WorkerJobV1, WorkerStatus
from app.modules.evidence_lifecycle.models import WorkerAvailabilityStatus


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceView(_ResponseModel):
    """Safe evidence metadata -- never a credential, presigned URL, or raw object bytes."""

    evidence_id: UUID
    case_id: UUID
    source_type: SourceType
    original_filename: str
    content_type: str
    sha256: str
    classification: EvidenceClassification
    uploaded_by: UUID
    uploaded_at: datetime
    parser_profile: str | None
    processing_status: EvidenceProcessingStatus
    created_at: datetime


class JobView(_ResponseModel):
    """Safe job status -- never queue/broker details, a claim token, or an object URI."""

    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    source_type: SourceType
    processor_name: str
    processor_version: str
    attempt: int
    status: WorkerStatus
    requested_at: datetime
    dispatched_at: datetime | None
    claimed_at: datetime | None
    completed_at: datetime | None
    observation_count: int
    last_error_code: str | None
    last_error_message: str | None
    #: The job's most recently accepted micro-batch progress event, if any
    #: (Phase 3) -- `None` for a job that never received a progress-bearing
    #: `ObservationBatchSubmissionV1`. Never a raw human-readable message.
    latest_progress: ObservationBatchProgressV1 | None = None


class EvidenceUploadResponse(_ResponseModel):
    evidence: EvidenceView
    job: JobView


class EvidenceListResponse(_ResponseModel):
    items: tuple[EvidenceView, ...]


# --- Internal worker-lifecycle shapes (never returned from a user-facing endpoint) ---


class ClaimRequest(_ResponseModel):
    """A worker's self-declaration of what it can process.

    Not a verified per-worker identity -- see
    `docs/architecture/worker-job-lifecycle.md`'s "Worker identity" section.
    """

    processor_name: str = Field(min_length=1)
    processor_version: str = Field(min_length=1)


class ClaimResponse(_ResponseModel):
    """The full `WorkerJobV1` a worker needs to process it, plus its one-time claim token.

    `job`/`claim_token`/`lease_expires_at` are all `None` when nothing is
    eligible right now -- a safe "no work" response, never an error.
    `claim_token` is transport metadata only, never part of `WorkerJobV1`.
    """

    job: WorkerJobV1 | None
    claim_token: str | None
    lease_expires_at: datetime | None


class ResultAcknowledgement(_ResponseModel):
    """Safe acknowledgement of an accepted worker result -- never an object URI or credential."""

    job_id: UUID
    status: WorkerStatus
    result_id: UUID
    observation_count: int
    observation_ids: tuple[UUID, ...]


class MediaManifestResponse(_ResponseModel):
    """Safe acknowledgement of a persisted coordinator manifest.

    `created=False` means an identical manifest (matching content hash)
    was already durably persisted -- an idempotent retry, not a conflict.
    Never echoes the manifest's own chunk definitions back: the worker
    that submitted it already has them (it built the manifest), and this
    is only proof the coordinator now owns a persisted copy under this ID.
    """

    manifest_id: UUID
    created: bool


class RenewLeaseResponse(_ResponseModel):
    """Safe acknowledgement of a lease renewal -- the new expiry only, nothing else."""

    job_id: UUID
    lease_expires_at: datetime


class WorkerHeartbeatResponse(_ResponseModel):
    """Safe worker availability acknowledgement; contains no network/token data."""

    job_id: UUID
    availability: WorkerAvailabilityStatus
    lease_expires_at: datetime
