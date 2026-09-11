"""In-memory duck-typed stand-in for `EvidenceLifecycleRepository`.

Implements the same async method signatures, including the real migration's
uniqueness constraints (`(case_id, upload_idempotency_key)` and
`worker_jobs.idempotency_key`), by raising `sqlalchemy.exc.IntegrityError`
just like a live PostgreSQL insert would -- so `service.py`'s
conflict-detection path is exercisable without a real database. See
`tests/integration/evidence_lifecycle/` for tests against a live one.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy.exc

from app.modules.evidence_lifecycle.models import EvidenceRecord, WorkerJobRecord


class FakeEvidenceLifecycleRepository:
    def __init__(self) -> None:
        self.evidence: dict[UUID, EvidenceRecord] = {}
        self.jobs: dict[UUID, WorkerJobRecord] = {}

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
