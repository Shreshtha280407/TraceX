"""PostgreSQL-facing repository for the graph-projection durable outbox.

Reads/claims/transitions `graph_projection_jobs` rows (defined and durably
inserted by `app.modules.evidence_lifecycle.repository`, in the same
transaction as an accepted worker result -- see that module's
`EvidenceLifecycleRepository.submit_result`) and reads the canonical
`worker_observations`/`evidence_records` rows needed to reconstruct the
`ObservationV1`/`EvidenceRecordV1` contracts `projection.py` projects.

This is the *only* module in `app/modules/graph/` that touches PostgreSQL --
`projection.py`/`queries.py`/`schema.py` only ever see Neo4j via
`Neo4jGraphRepository`. `app.modules.graph` is explicitly a "backend-owned
internal service" (unlike the DB-blind extraction workers in
`structured_processing`/`communication_processing`/`media_processing`), so
a direct PostgreSQL connection here is by design, not a boundary violation
-- see `docs/architecture/graph-projection.md`. Importing `evidence_lifecycle
.repository`'s table objects is a one-directional dependency: this module
consumes that module's accepted results, and `evidence_lifecycle` never
imports `app.modules.graph` back (statically enforced -- see
`tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`).

## Attempt/lease/retry policy

Unlike `evidence_lifecycle.repository.claim_job` (whose `attempt` only
increments on a lease-expiry *reclaim*, never on a first claim, and has no
max-attempt cutoff at all), `attempt` here increments on *every* claim,
including the first -- because this table's `max_attempts` genuinely bounds
total retries (see `claim_batch`), so "how many times has this actually
been tried" is the number that matters, not "how many times was it
reclaimed after abandonment."

`claim_batch` also defensively sweeps any `RUNNING` row whose lease expired
*and* which already exhausted `max_attempts` (a projector crash mid-attempt,
on what was already its last allowed attempt) to `FAILED` before claiming
anything -- without this, such a row would sit in `RUNNING` forever: never
reclaimable (correctly, since it's out of attempts) but also never marked
terminal, invisible to the "retained as failed for operator inspection"
guarantee this module makes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.contracts.evidence import EvidenceRecordV1
from app.contracts.observation import ObservationV1
from app.core.config import Settings
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    graph_projection_jobs_table,
    worker_observations_table,
)
from app.modules.graph.models import (
    CLAIMABLE_PROJECTION_JOB_STATUSES,
    GraphProjectionJobRecord,
    GraphProjectionJobStatus,
)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async PostgreSQL engine from application configuration.

    A separate engine/connection pool from `evidence_lifecycle`'s own --
    this module is a distinct process (`uv run python -m
    app.modules.graph.worker --once`) in normal operation, and even when
    imported in-process (tests, the read API) keeps its own connection
    lifecycle rather than reaching into another module's engine instance.
    """
    return create_async_engine(str(settings.postgres_dsn))


def _job_from_row(row: sa.RowMapping) -> GraphProjectionJobRecord:
    return GraphProjectionJobRecord.model_validate(dict(row))


def _observation_from_row(row: sa.RowMapping) -> ObservationV1:
    payload: dict[str, object] = dict(row["canonical_payload"])
    return ObservationV1.model_validate(payload)


def _evidence_from_row(row: sa.RowMapping) -> EvidenceRecordV1:
    return EvidenceRecordV1(
        evidence_id=row["evidence_id"],
        case_id=row["case_id"],
        source_type=row["source_type"],
        original_filename=row["original_filename"],
        content_type=row["content_type"],
        object_uri=row["object_uri"],
        sha256=row["sha256"],
        classification=row["classification"],
        uploaded_by=str(row["uploaded_by"]),
        uploaded_at=row["uploaded_at"],
        parser_profile=row["parser_profile"],
        processing_status=row["processing_status"],
    )


class GraphProjectionOutboxRepository:
    """Typed async persistence for the durable graph-projection queue.

    Accepts an injected `AsyncEngine` (see `create_engine`) so tests can
    point it at a real ephemeral test database; there is no in-memory fake
    for this repository -- unit tests exercise `app.modules.graph.projector`
    (the orchestration logic) against a minimal duck-typed fake of *this*
    class's interface, and `tests/integration/graph/` proves the real
    `FOR UPDATE SKIP LOCKED` concurrency behavior against live PostgreSQL.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def claim_batch(
        self, *, now: datetime, lease_seconds: int, batch_size: int
    ) -> list[GraphProjectionJobRecord]:
        """Atomically claim up to `batch_size` eligible jobs, oldest-first.

        Eligible: `status` is `queued`/`deferred`, or `status` is `running`
        with an expired lease -- in every case, only while `attempt <
        max_attempts` (a row that has already exhausted its attempts is
        never reclaimed; see the module docstring for the crash-recovery
        sweep this method runs first). `FOR UPDATE SKIP LOCKED` so two
        concurrent projector runs never claim the same row twice, mirroring
        `evidence_lifecycle.repository.claim_job`'s own concurrency pattern.
        """
        claimable_statuses = tuple(s.value for s in CLAIMABLE_PROJECTION_JOB_STATUSES)
        async with self._engine.begin() as conn:
            # Crash-recovery sweep: a RUNNING row with an expired lease that
            # already used its last attempt is stuck, not retryable -- make
            # that terminal and inspectable rather than leaving it silently
            # invisible to both "queued for retry" and "failed" views.
            await conn.execute(
                sa.update(graph_projection_jobs_table)
                .where(
                    graph_projection_jobs_table.c.status == GraphProjectionJobStatus.RUNNING.value,
                    graph_projection_jobs_table.c.lease_expires_at < now,
                    graph_projection_jobs_table.c.attempt
                    >= graph_projection_jobs_table.c.max_attempts,
                )
                .values(
                    status=GraphProjectionJobStatus.FAILED.value,
                    lease_expires_at=None,
                    last_error_code="lease_expired_attempts_exhausted",
                    last_error_message=(
                        "projection lease expired with no completion and no attempts remaining"
                    ),
                    updated_at=now,
                    completed_at=now,
                )
            )

            select_stmt = (
                sa.select(graph_projection_jobs_table)
                .where(
                    sa.or_(
                        graph_projection_jobs_table.c.status.in_(claimable_statuses),
                        sa.and_(
                            graph_projection_jobs_table.c.status
                            == GraphProjectionJobStatus.RUNNING.value,
                            graph_projection_jobs_table.c.lease_expires_at < now,
                        ),
                    ),
                    graph_projection_jobs_table.c.attempt
                    < graph_projection_jobs_table.c.max_attempts,
                )
                .order_by(graph_projection_jobs_table.c.created_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await conn.execute(select_stmt)).mappings().all()
            if not rows:
                return []

            lease_expires_at = now + timedelta(seconds=lease_seconds)
            claimed: list[GraphProjectionJobRecord] = []
            for row in rows:
                new_attempt = row["attempt"] + 1
                await conn.execute(
                    sa.update(graph_projection_jobs_table)
                    .where(graph_projection_jobs_table.c.projection_id == row["projection_id"])
                    .values(
                        status=GraphProjectionJobStatus.RUNNING.value,
                        attempt=new_attempt,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                )
                claimed.append(
                    GraphProjectionJobRecord(
                        projection_id=row["projection_id"],
                        case_id=row["case_id"],
                        evidence_id=row["evidence_id"],
                        observation_id=row["observation_id"],
                        status=GraphProjectionJobStatus.RUNNING,
                        attempt=new_attempt,
                        max_attempts=row["max_attempts"],
                        lease_expires_at=lease_expires_at,
                        last_error_code=row["last_error_code"],
                        last_error_message=row["last_error_message"],
                        created_at=row["created_at"],
                        updated_at=now,
                        completed_at=None,
                    )
                )
            return claimed

    async def get_job(self, projection_id: UUID) -> GraphProjectionJobRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(graph_projection_jobs_table).where(
                            graph_projection_jobs_table.c.projection_id == projection_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    async def get_observation(self, observation_id: UUID) -> ObservationV1 | None:
        """The canonical `ObservationV1` a projection job refers to, or `None` if missing."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.observation_id == observation_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _observation_from_row(row) if row is not None else None

    async def get_evidence(self, case_id: UUID, evidence_id: UUID) -> EvidenceRecordV1 | None:
        """The canonical `EvidenceRecordV1` a projection job's evidence, or `None` if missing."""
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

    async def mark_succeeded(self, projection_id: UUID, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(graph_projection_jobs_table)
                .where(graph_projection_jobs_table.c.projection_id == projection_id)
                .values(
                    status=GraphProjectionJobStatus.SUCCEEDED.value,
                    lease_expires_at=None,
                    last_error_code=None,
                    last_error_message=None,
                    updated_at=now,
                    completed_at=now,
                )
            )

    async def mark_retryable_failure(
        self,
        projection_id: UUID,
        *,
        now: datetime,
        error_code: str,
        error_message: str,
        requeue_status: GraphProjectionJobStatus,
    ) -> None:
        """Requeue the job (if attempts remain) or leave it durably `FAILED`.

        `error_message` must already be sanitized by the caller (see
        `app.modules.graph.projector`) -- never a raw driver exception
        string, Cypher text, or stack trace.
        """
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(
                            graph_projection_jobs_table.c.attempt,
                            graph_projection_jobs_table.c.max_attempts,
                        )
                        .where(graph_projection_jobs_table.c.projection_id == projection_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            exhausted = row["attempt"] >= row["max_attempts"]
            await conn.execute(
                sa.update(graph_projection_jobs_table)
                .where(graph_projection_jobs_table.c.projection_id == projection_id)
                .values(
                    status=(
                        GraphProjectionJobStatus.FAILED.value if exhausted else requeue_status.value
                    ),
                    lease_expires_at=None,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    updated_at=now,
                    completed_at=now if exhausted else None,
                )
            )

    async def mark_failed(
        self, projection_id: UUID, *, now: datetime, error_code: str, error_message: str
    ) -> None:
        """Non-retryable failure (e.g. the canonical observation/evidence row is missing).

        Terminal immediately, regardless of remaining attempts.
        `error_message` must already be sanitized by the caller.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(graph_projection_jobs_table)
                .where(graph_projection_jobs_table.c.projection_id == projection_id)
                .values(
                    status=GraphProjectionJobStatus.FAILED.value,
                    lease_expires_at=None,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    updated_at=now,
                    completed_at=now,
                )
            )
