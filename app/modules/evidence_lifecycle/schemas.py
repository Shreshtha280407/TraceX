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

from pydantic import BaseModel, ConfigDict

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.worker import WorkerStatus


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
    """Safe job status -- never the queue/broker connection details."""

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
    last_error_code: str | None
    last_error_message: str | None


class EvidenceUploadResponse(_ResponseModel):
    evidence: EvidenceView
    job: JobView


class EvidenceListResponse(_ResponseModel):
    items: tuple[EvidenceView, ...]
