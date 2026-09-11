"""Typed async PostgreSQL repository for the evidence-lifecycle module.

Mirrors `app.modules.access_control.repository`'s conventions exactly:
hand-written `sqlalchemy.Table` objects (this module does not import the
migration, and `migrations/env.py` keeps `target_metadata = None` -- see
the note at the top of
`migrations/versions/<rev>_evidence_lifecycle_foundation.py`), every
statement built with SQLAlchemy Core's expression language, values always
bound parameters. Keep the two in sync when either changes.

Every query here is scoped by `case_id` at the WHERE-clause level, not only
enforced by the caller -- case isolation is structural, not a convention an
API route could accidentally skip.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.contracts.worker import WorkerStatus
from app.core.config import Settings
from app.modules.evidence_lifecycle.models import (
    EvidenceRecord,
    ObservationRecord,
    WorkerJobRecord,
    WorkerResultRecord,
)


def _dump_for_insert(model: BaseModel, enum_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """`model.model_dump(mode="python")`, with named enum fields reduced to their plain `.value`.

    See `app.modules.access_control.repository._dump_for_insert` for why.
    """
    values = model.model_dump(mode="python")
    for field in enum_fields:
        values[field] = values[field].value
    return values


metadata = sa.MetaData()

evidence_records_table = sa.Table(
    "evidence_records",
    metadata,
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("source_type", sa.Text(), nullable=False),
    sa.Column("original_filename", sa.Text(), nullable=False),
    sa.Column("content_type", sa.Text(), nullable=False),
    sa.Column("object_uri", sa.Text(), nullable=False),
    sa.Column("sha256", sa.Text(), nullable=False),
    sa.Column("classification", sa.Text(), nullable=False),
    sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("parser_profile", sa.Text(), nullable=True),
    sa.Column("processing_status", sa.Text(), nullable=False),
    sa.Column("upload_idempotency_key", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

worker_jobs_table = sa.Table(
    "worker_jobs",
    metadata,
    sa.Column("job_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("source_type", sa.Text(), nullable=False),
    sa.Column("processor_name", sa.Text(), nullable=False),
    sa.Column("processor_version", sa.Text(), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("input_object_uri", sa.Text(), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("claimed_by", sa.Text(), nullable=True),
    sa.Column("claim_token_hash", sa.Text(), nullable=True),
    sa.Column("last_error_code", sa.Text(), nullable=True),
    sa.Column("last_error_message", sa.Text(), nullable=True),
    sa.Column("last_error_retryable", sa.Boolean(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

worker_results_table = sa.Table(
    "worker_results",
    metadata,
    sa.Column("result_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("derived_artifacts", postgresql.JSONB(), nullable=False),
    sa.Column("checkpoint", sa.Text(), nullable=True),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("error_retryable", sa.Boolean(), nullable=True),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("payload_hash", sa.Text(), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

worker_observations_table = sa.Table(
    "worker_observations",
    metadata,
    sa.Column("observation_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observation_type", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async PostgreSQL engine from application configuration."""
    return create_async_engine(str(settings.postgres_dsn))


def _evidence_from_row(row: sa.RowMapping) -> EvidenceRecord:
    return EvidenceRecord.model_validate(dict(row))


def _job_from_row(row: sa.RowMapping) -> WorkerJobRecord:
    return WorkerJobRecord.model_validate(dict(row))


def _result_from_row(row: sa.RowMapping) -> WorkerResultRecord:
    return WorkerResultRecord.model_validate(dict(row))


def _observation_from_row(row: sa.RowMapping) -> ObservationRecord:
    return ObservationRecord.model_validate(dict(row))


class EvidenceLifecycleRepository:
    """Typed async persistence for evidence metadata and durable worker jobs.

    Accepts an injected `AsyncEngine` (see `create_engine`) so tests can
    point it at a real ephemeral test database; there is no in-memory fake
    for this repository -- the integration tests self-skip instead when
    PostgreSQL isn't reachable (see `tests/integration/evidence_lifecycle/`).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    # --- evidence + job (single atomic write path) -----------------------

    async def create_evidence_with_job(
        self, evidence: EvidenceRecord, job: WorkerJobRecord
    ) -> None:
        """Insert the evidence row and its job row in one transaction.

        Atomic on purpose: an evidence row must never exist without its
        corresponding job row (or vice versa). A unique-constraint
        violation (concurrent duplicate `Idempotency-Key`, or a duplicate
        `worker_jobs.idempotency_key`) raises `sqlalchemy.exc.IntegrityError`
        and rolls the whole transaction back -- `service.py` handles that.
        """
        evidence_values = _dump_for_insert(
            evidence, ("source_type", "classification", "processing_status")
        )
        job_values = _dump_for_insert(job, ("source_type", "status"))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(evidence_records_table).values(**evidence_values))
            await conn.execute(sa.insert(worker_jobs_table).values(**job_values))

    # --- evidence (read) ---------------------------------------------------

    async def get_evidence(self, case_id: UUID, evidence_id: UUID) -> EvidenceRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table).where(
                            evidence_records_table.c.case_id == case_id,
                            evidence_records_table.c.evidence_id == evidence_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _evidence_from_row(row) if row is not None else None

    async def list_evidence(
        self, case_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[EvidenceRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table)
                        .where(evidence_records_table.c.case_id == case_id)
                        .order_by(evidence_records_table.c.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
        return [_evidence_from_row(row) for row in rows]

    async def get_evidence_by_idempotency_key(
        self, case_id: UUID, upload_idempotency_key: str
    ) -> EvidenceRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table).where(
                            evidence_records_table.c.case_id == case_id,
                            evidence_records_table.c.upload_idempotency_key
                            == upload_idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _evidence_from_row(row) if row is not None else None

    # --- jobs (read + dispatch bookkeeping) ---------------------------------

    async def get_job(self, case_id: UUID, job_id: UUID) -> WorkerJobRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(
                            worker_jobs_table.c.case_id == case_id,
                            worker_jobs_table.c.job_id == job_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    async def get_job_by_evidence(self, case_id: UUID, evidence_id: UUID) -> WorkerJobRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(
                            worker_jobs_table.c.case_id == case_id,
                            worker_jobs_table.c.evidence_id == evidence_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    async def mark_job_dispatched(self, job_id: UUID, dispatched_at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(worker_jobs_table)
                .where(worker_jobs_table.c.job_id == job_id)
                .values(dispatched_at=dispatched_at, updated_at=dispatched_at)
            )

    async def get_job_by_id(self, job_id: UUID) -> WorkerJobRecord | None:
        """Unscoped-by-case lookup for the internal worker endpoints.

        A worker authenticates by holding a valid claim token for a
        specific `job_id`, not by case membership -- see
        `docs/architecture/worker-job-lifecycle.md`'s "Worker identity"
        section. Never exposed to a case-scoped user-facing caller.
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(worker_jobs_table.c.job_id == job_id)
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    # --- job claim (worker lifecycle) --------------------------------------

    async def claim_job(
        self,
        *,
        processor_name: str,
        processor_version: str,
        now: datetime,
        lease_seconds: int,
        claim_token_hash: str,
    ) -> tuple[WorkerJobRecord, bool] | None:
        """Atomically claim one eligible job for `processor_name`/`processor_version`.

        Eligible means either genuinely `queued`, or `running` with an
        expired lease (a prior claim that was never resolved). `FOR UPDATE
        SKIP LOCKED` makes two concurrent callers structurally unable to
        claim the same row: the loser's `SELECT` simply skips the winner's
        locked row and finds a different eligible job (or none).

        Returns `(claimed_job, was_reclaim)`, or `None` if nothing eligible
        exists right now. `was_reclaim` is `True` only when an expired
        lease was recovered (`attempt` is incremented in that case, never
        on a job's first-ever claim).
        """
        async with self._engine.begin() as conn:
            candidate = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table)
                        .where(
                            worker_jobs_table.c.processor_name == processor_name,
                            worker_jobs_table.c.processor_version == processor_version,
                            sa.or_(
                                worker_jobs_table.c.status == WorkerStatus.QUEUED.value,
                                sa.and_(
                                    worker_jobs_table.c.status == WorkerStatus.RUNNING.value,
                                    worker_jobs_table.c.lease_expires_at < now,
                                ),
                            ),
                        )
                        .order_by(worker_jobs_table.c.requested_at.asc())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .first()
            )
            if candidate is None:
                return None

            was_reclaim = candidate["status"] == WorkerStatus.RUNNING.value
            new_attempt = candidate["attempt"] + 1 if was_reclaim else candidate["attempt"]
            lease_expires_at = now + timedelta(seconds=lease_seconds)

            await conn.execute(
                sa.update(worker_jobs_table)
                .where(worker_jobs_table.c.job_id == candidate["job_id"])
                .values(
                    status=WorkerStatus.RUNNING.value,
                    attempt=new_attempt,
                    claimed_at=now,
                    lease_expires_at=lease_expires_at,
                    claimed_by=processor_name,
                    claim_token_hash=claim_token_hash,
                    updated_at=now,
                )
            )
            updated = dict(candidate)
            updated.update(
                status=WorkerStatus.RUNNING.value,
                attempt=new_attempt,
                claimed_at=now,
                lease_expires_at=lease_expires_at,
                claimed_by=processor_name,
                claim_token_hash=claim_token_hash,
                updated_at=now,
            )
            return WorkerJobRecord.model_validate(updated), was_reclaim

    # --- worker result (single atomic write path) ---------------------------

    async def get_result_for_job(self, job_id: UUID) -> WorkerResultRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_results_table).where(
                            worker_results_table.c.job_id == job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _result_from_row(row) if row is not None else None

    async def list_observations_for_result(self, result_id: UUID) -> list[ObservationRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.result_id == result_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_observation_from_row(row) for row in rows]

    async def count_observations_for_job(self, job_id: UUID) -> int:
        async with self._engine.connect() as conn:
            count = await conn.scalar(
                sa.select(sa.func.count())
                .select_from(worker_observations_table)
                .where(worker_observations_table.c.job_id == job_id)
            )
        return count or 0

    async def submit_result(
        self,
        *,
        job_id: UUID,
        expected_claim_token_hash: str,
        result: WorkerResultRecord,
        observations: list[ObservationRecord],
    ) -> None:
        """Insert the result + its observations and mark the job terminal, in one transaction.

        Guarded by `job_id` + the caller's already-verified claim token
        hash on the `UPDATE ... WHERE` clause as defense in depth; the
        primary concurrency-safety mechanism is `worker_results.job_id`'s
        unique constraint -- a losing concurrent submission raises
        `sqlalchemy.exc.IntegrityError` here, exactly like
        `create_evidence_with_job`'s idempotency-key race, and `service.py`
        handles it the same way (re-read, compare, replay-or-conflict).
        """
        result_values = _dump_for_insert(result, ("status",))
        observation_values = [_dump_for_insert(o) for o in observations]
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(worker_results_table).values(**result_values))
            if observation_values:
                await conn.execute(sa.insert(worker_observations_table), observation_values)
            await conn.execute(
                sa.update(worker_jobs_table)
                .where(
                    worker_jobs_table.c.job_id == job_id,
                    worker_jobs_table.c.claim_token_hash == expected_claim_token_hash,
                )
                .values(
                    status=result.status.value,
                    last_error_code=result.error_code,
                    last_error_message=result.error_message,
                    last_error_retryable=result.error_retryable,
                    updated_at=result.updated_at,
                )
            )
