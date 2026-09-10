"""Worker job/result/progress envelopes.

These are the only contract a future worker needs: it receives a
`WorkerJobV1`, fetches `input_object_uri` from object storage, and returns
canonical `ObservationV1`s in a `WorkerResultV1`. A worker never needs
direct PostgreSQL, Neo4j, or MinIO *credentials* — object storage access is
via the pre-scoped `input_object_uri`/`object_uri` values it is handed, and
persistence of results into Postgres/Neo4j is the API/orchestrator's job,
not the worker's.

Idempotency key format: `"{case_id}:{evidence_id}:{processor_name}:{processor_version}"`
(colon-separated, each segment `[A-Za-z0-9_.-]+` — dots are allowed so
semantic-version processor versions like `1.0.0` fit). The key
intentionally excludes `attempt`, so retries of the same logical job reuse
the same key and a consumer can safely deduplicate re-delivered results.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.contracts.common import ContractVersion, TraceXModel
from app.contracts.evidence import SourceType
from app.contracts.observation import ObservationV1

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+){3}$")


class WorkerStatus(StrEnum):
    """Lifecycle status of a worker job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"


class WorkerJobV1(TraceXModel):
    """A unit of work dispatched to a source-processing worker."""

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    source_type: SourceType
    processor_name: str = Field(min_length=1)
    processor_version: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    idempotency_key: str
    input_object_uri: str = Field(min_length=1)
    requested_at: AwareDatetime

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, value: str) -> str:
        if not IDEMPOTENCY_KEY_PATTERN.match(value):
            raise ValueError(
                "idempotency_key must match "
                "'{case_id}:{evidence_id}:{processor_name}:{processor_version}'"
            )
        return value


class WorkerError(TraceXModel):
    """A safe, structured, non-secret error description."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class DerivedArtifact(TraceXModel):
    """A byproduct a worker produced (thumbnail, transcript blob, ...).

    Referenced by URI only — artifact bytes are never embedded inline.
    """

    artifact_type: str = Field(min_length=1)
    object_uri: str = Field(min_length=1)
    content_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WorkerResultV1(TraceXModel):
    """The outcome of a worker job: canonical observations plus status."""

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    status: WorkerStatus
    observations: list[ObservationV1] = Field(default_factory=list)
    derived_artifacts: list[DerivedArtifact] = Field(default_factory=list)
    checkpoint: str | None = None
    error: WorkerError | None = None
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> WorkerResultV1:
        if self.status is WorkerStatus.FAILED and self.error is None:
            raise ValueError("a failed WorkerResultV1 must include an error")
        if self.status is WorkerStatus.SUCCEEDED and self.error is not None:
            raise ValueError("a succeeded WorkerResultV1 must not include an error")
        return self


class WorkerProgressV1(TraceXModel):
    """An optional intermediate progress report for a long-running job."""

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    job_id: UUID
    case_id: UUID
    status: WorkerStatus
    progress_pct: float = Field(ge=0.0, le=100.0)
    message: str | None = None
    reported_at: AwareDatetime
