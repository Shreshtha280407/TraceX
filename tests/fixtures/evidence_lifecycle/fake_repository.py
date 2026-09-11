"""In-memory duck-typed stand-in for `EvidenceLifecycleRepository`.

Implements the same async method signatures, including the real migration's
uniqueness constraints (`(case_id, upload_idempotency_key)` and
`worker_jobs.idempotency_key`), by raising `sqlalchemy.exc.IntegrityError`
just like a live PostgreSQL insert would -- so `service.py`'s
conflict-detection path is exercisable without a real database. See
`tests/integration/evidence_lifecycle/` for tests against a live one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy.exc

from app.contracts.worker import WorkerStatus
from app.modules.evidence_lifecycle.models import (
    EvidenceRecord,
    ObservationRecord,
    WorkerJobRecord,
    WorkerResultRecord,
)


@dataclass
class FakeGraphProjectionJob:
    """Shape-only stand-in for a `graph_projection_jobs` row.

    Just enough fields to let `evidence_lifecycle`'s own tests assert that
    one row is durably enqueued per accepted observation -- the real
    claim/lease/retry lifecycle over this table is `app/modules/graph/`'s
    responsibility, tested against its own fixtures.
    """

    projection_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    status: str
    attempt: int
    max_attempts: int


class FakeEvidenceLifecycleRepository:
    def __init__(self) -> None:
        self.evidence: dict[UUID, EvidenceRecord] = {}
        self.jobs: dict[UUID, WorkerJobRecord] = {}
        self.results: dict[UUID, WorkerResultRecord] = {}
        self.observations: dict[UUID, ObservationRecord] = {}
        self.graph_projection_jobs: dict[UUID, FakeGraphProjectionJob] = {}

    async def close(self) -> None:
        pass

    async def create_evidence_with_job(
        self, evidence: EvidenceRecord, job: WorkerJobRecord
    ) -> None:
        if evidence.upload_idempotency_key is not None and any(
            e.case_id == evidence.case_id
            and e.upload_idempotency_key == evidence.upload_idempotency_key
            for e in self.evidence.values()
        ):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate upload_idempotency_key", {}, Exception("unique violation")
            )
        if any(j.idempotency_key == job.idempotency_key for j in self.jobs.values()):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate worker_jobs.idempotency_key", {}, Exception("unique violation")
            )
        self.evidence[evidence.evidence_id] = evidence
        self.jobs[job.job_id] = job

    async def get_evidence(self, case_id: UUID, evidence_id: UUID) -> EvidenceRecord | None:
        record = self.evidence.get(evidence_id)
        return record if record is not None and record.case_id == case_id else None

    async def list_evidence(
        self, case_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[EvidenceRecord]:
        records = sorted(
            (e for e in self.evidence.values() if e.case_id == case_id),
            key=lambda e: e.created_at,
            reverse=True,
        )
        return records[offset : offset + limit]

    async def get_evidence_by_idempotency_key(
        self, case_id: UUID, upload_idempotency_key: str
    ) -> EvidenceRecord | None:
        return next(
            (
                e
                for e in self.evidence.values()
                if e.case_id == case_id and e.upload_idempotency_key == upload_idempotency_key
            ),
            None,
        )

    async def get_job(self, case_id: UUID, job_id: UUID) -> WorkerJobRecord | None:
        job = self.jobs.get(job_id)
        return job if job is not None and job.case_id == case_id else None

    async def get_job_by_evidence(self, case_id: UUID, evidence_id: UUID) -> WorkerJobRecord | None:
        return next(
            (
                j
                for j in self.jobs.values()
                if j.case_id == case_id and j.evidence_id == evidence_id
            ),
            None,
        )

    async def mark_job_dispatched(self, job_id: UUID, dispatched_at: datetime) -> None:
        job = self.jobs.get(job_id)
        if job is not None:
            self.jobs[job_id] = job.model_copy(
                update={"dispatched_at": dispatched_at, "updated_at": dispatched_at}
            )

    async def get_job_by_id(self, job_id: UUID) -> WorkerJobRecord | None:
        return self.jobs.get(job_id)

    # --- job claim (worker lifecycle) --------------------------------------

    async def claim_job(
        self,
        *,
        processor_name: str,
        processor_version: str,
        now: datetime,
        lease_seconds: int,
        claim_token_hash: str,
        claimed_by_worker_id: UUID | None = None,
    ) -> tuple[WorkerJobRecord, bool] | None:
        eligible = sorted(
            (
                job
                for job in self.jobs.values()
                if job.processor_name == processor_name
                and job.processor_version == processor_version
                and (
                    job.status is WorkerStatus.QUEUED
                    or (
                        job.status is WorkerStatus.RUNNING
                        and job.lease_expires_at is not None
                        and job.lease_expires_at < now
                    )
                )
            ),
            key=lambda job: job.requested_at,
        )
        if not eligible:
            return None
        candidate = eligible[0]
        was_reclaim = candidate.status is WorkerStatus.RUNNING
        new_attempt = candidate.attempt + 1 if was_reclaim else candidate.attempt
        claimed = candidate.model_copy(
            update={
                "status": WorkerStatus.RUNNING,
                "attempt": new_attempt,
                "claimed_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "claimed_by": processor_name,
                "claimed_by_worker_id": claimed_by_worker_id,
                "claim_token_hash": claim_token_hash,
                "updated_at": now,
            }
        )
        self.jobs[claimed.job_id] = claimed
        return claimed, was_reclaim

    async def renew_lease(
        self, job_id: UUID, *, now: datetime, lease_seconds: int
    ) -> datetime | None:
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.status is not WorkerStatus.RUNNING
            or job.lease_expires_at is None
            or job.lease_expires_at < now
        ):
            return None
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.jobs[job_id] = job.model_copy(
            update={"lease_expires_at": lease_expires_at, "updated_at": now}
        )
        return lease_expires_at

    # --- worker result -------------------------------------------------------

    async def get_result_for_job(self, job_id: UUID) -> WorkerResultRecord | None:
        return next((r for r in self.results.values() if r.job_id == job_id), None)

    async def list_observations_for_result(self, result_id: UUID) -> list[ObservationRecord]:
        return [o for o in self.observations.values() if o.result_id == result_id]

    async def count_observations_for_job(self, job_id: UUID) -> int:
        return sum(1 for o in self.observations.values() if o.job_id == job_id)

    async def submit_result(
        self,
        *,
        job_id: UUID,
        expected_claim_token_hash: str,
        result: WorkerResultRecord,
        observations: list[ObservationRecord],
        graph_projection_max_attempts: int,
    ) -> None:
        if any(r.job_id == result.job_id for r in self.results.values()):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate worker_results.job_id", {}, Exception("unique violation")
            )
        job = self.jobs.get(job_id)
        if job is None or job.claim_token_hash != expected_claim_token_hash:
            return
        self.results[result.result_id] = result
        for observation in observations:
            self.observations[observation.observation_id] = observation
            self.graph_projection_jobs[observation.observation_id] = FakeGraphProjectionJob(
                projection_id=uuid4(),
                case_id=observation.case_id,
                evidence_id=observation.evidence_id,
                observation_id=observation.observation_id,
                status="queued",
                attempt=0,
                max_attempts=graph_projection_max_attempts,
            )
        self.jobs[job_id] = job.model_copy(
            update={
                "status": result.status,
                "last_error_code": result.error_code,
                "last_error_message": result.error_message,
                "last_error_retryable": result.error_retryable,
                "updated_at": result.updated_at,
            }
        )
