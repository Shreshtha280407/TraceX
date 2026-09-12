"""Builds `ObservationBatchSubmissionV1`s for Nipun's micro-batch endpoint.

Mirrors `app.modules.structured_processing.batching` in shape (deterministic
`batch_id`/`idempotency_key`, shared builders for
`TransformationProvenanceV1`/`ObservationBatchProgressV1`/
`ObservationBatchSubmissionV1`) but owns its own copy rather than importing
it — the same independently-buildable-module convention `client.py`/
`input_resolver.py` already established for this module.

`batch_id`/`idempotency_key` are both derived deterministically from
`(job_id, batch_sequence)` — retrying the exact same logical batch (e.g.
after a transport failure) always produces the exact same two tokens, so
the server's own idempotent-replay rule (same `batch_id` -> replay, never a
duplicate) works correctly without this module tracking any retry state of
its own.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.contracts.observation import ObservationV1
from app.contracts.observation_batch import (
    ObservationBatchProgressV1,
    ObservationBatchSubmissionV1,
    TransformationProvenanceV1,
    TransformationStatus,
)
from app.contracts.worker import WorkerJobV1
from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid


def deterministic_batch_id(*, job_id: UUID, batch_sequence: int) -> str:
    return f"batch-{batch_sequence:08d}"


def deterministic_idempotency_key(*, job_id: UUID, batch_sequence: int) -> str:
    return canonical_sha256({"job_id": str(job_id), "batch_sequence": batch_sequence})


def build_progress(
    *,
    stage: str,
    units_completed: int,
    observations_emitted: int,
    batch_sequence: int,
    occurred_at: datetime,
    units_total: int | None = None,
    message_code: str | None = None,
) -> ObservationBatchProgressV1:
    return ObservationBatchProgressV1(
        stage=stage,
        units_total=units_total,
        units_completed=units_completed,
        observations_emitted=observations_emitted,
        batch_sequence=batch_sequence,
        message_code=message_code,
        occurred_at=occurred_at,
    )


def build_transformation(
    *,
    job: WorkerJobV1,
    batch_id: str,
    ordinal: int,
    step_name: str,
    step_version: str,
    config_hash: str,
    started_at: datetime,
    completed_at: datetime,
    status: TransformationStatus = TransformationStatus.SUCCEEDED,
    model_version: str = "n/a",
    input_locator: SourceLocator | None = None,
    output_observation_ids: list[UUID] | None = None,
    safe_metadata: dict[str, JsonValue] | None = None,
) -> TransformationProvenanceV1:
    transformation_id = deterministic_uuid(
        str(job.job_id), batch_id, str(ordinal), step_name, step_version
    )
    return TransformationProvenanceV1(
        transformation_id=transformation_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        job_id=job.job_id,
        batch_id=batch_id,
        ordinal=ordinal,
        step_name=step_name,
        step_version=step_version,
        config_hash=config_hash,
        model_version=model_version,
        input_locator=input_locator,
        output_observation_ids=output_observation_ids or [],
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        safe_metadata=safe_metadata or {},
    )


def build_batch_submission(
    *,
    job: WorkerJobV1,
    batch_sequence: int,
    submitted_at: datetime,
    observations: list[ObservationV1] | None = None,
    transformations: list[TransformationProvenanceV1] | None = None,
    progress: ObservationBatchProgressV1 | None = None,
    is_final_batch: bool = False,
) -> ObservationBatchSubmissionV1:
    batch_id = deterministic_batch_id(job_id=job.job_id, batch_sequence=batch_sequence)
    return ObservationBatchSubmissionV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id=batch_id,
        batch_sequence=batch_sequence,
        idempotency_key=deterministic_idempotency_key(
            job_id=job.job_id, batch_sequence=batch_sequence
        ),
        observations=observations or [],
        transformations=transformations or [],
        progress=progress,
        submitted_at=submitted_at,
        is_final_batch=is_final_batch,
    )


__all__ = [
    "build_batch_submission",
    "build_progress",
    "build_transformation",
    "deterministic_batch_id",
    "deterministic_idempotency_key",
]
