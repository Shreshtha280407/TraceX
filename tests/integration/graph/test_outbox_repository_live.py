"""Live PostgreSQL tests for `app/modules/graph/outbox_repository.py`.

Proves what an in-memory fake fundamentally can't: real `FOR UPDATE SKIP
LOCKED` concurrency safety, real lease-expiry reclaim, and real retry
exhaustion against the actual `graph_projection_jobs` table and its
constraints. Self-skips (never fabricates a pass) if PostgreSQL isn't
reachable -- see `conftest.py`'s `outbox_repository` fixture.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa

from app.modules.evidence_lifecycle.repository import graph_projection_jobs_table
from app.modules.graph.models import GraphProjectionJobStatus
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository

_NOW = datetime.now(UTC)


async def _insert_job(
    outbox: GraphProjectionOutboxRepository,
    *,
    status: str = "queued",
    attempt: int = 0,
    max_attempts: int = 5,
    lease_expires_at: datetime | None = None,
    case_id: UUID | None = None,
    evidence_id: UUID | None = None,
    observation_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID, UUID]:
    projection_id = uuid4()
    case_id = case_id or uuid4()
    evidence_id = evidence_id or uuid4()
    observation_id = observation_id or uuid4()
    async with outbox._engine.begin() as conn:  # noqa: SLF001 - test-only direct insert
        await conn.execute(
            sa.insert(graph_projection_jobs_table).values(
                projection_id=projection_id,
                case_id=case_id,
                evidence_id=evidence_id,
                observation_id=observation_id,
                status=status,
                attempt=attempt,
                max_attempts=max_attempts,
                lease_expires_at=lease_expires_at,
                last_error_code=None,
                last_error_message=None,
                created_at=_NOW,
                updated_at=_NOW,
                completed_at=None,
            )
        )
    return projection_id, case_id, evidence_id, observation_id


async def _cleanup(outbox: GraphProjectionOutboxRepository, projection_ids: list) -> None:
    async with outbox._engine.begin() as conn:  # noqa: SLF001 - test-only cleanup
        await conn.execute(
            sa.delete(graph_projection_jobs_table).where(
                graph_projection_jobs_table.c.projection_id.in_(projection_ids)
            )
        )


async def test_claim_batch_is_concurrency_safe_across_two_concurrent_callers(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    """Two concurrent `claim_batch` calls never claim the same row -- real `SKIP LOCKED`."""
    ids = []
    for _ in range(4):
        projection_id, *_ = await _insert_job(outbox_repository)
        ids.append(projection_id)
    try:
        results = await asyncio.gather(
            outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=2),
            outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=2),
        )
        claimed_ids = [job.projection_id for batch in results for job in batch]
        assert len(claimed_ids) == len(set(claimed_ids)), "the same job was claimed twice"
        assert len(claimed_ids) == 4  # all four claimed across the two concurrent calls
    finally:
        await _cleanup(outbox_repository, ids)


async def test_claim_sets_running_status_lease_and_increments_attempt(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    projection_id, case_id, evidence_id, observation_id = await _insert_job(outbox_repository)
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        (job,) = [j for j in claimed if j.projection_id == projection_id]
        assert job.status is GraphProjectionJobStatus.RUNNING
        assert job.attempt == 1
        assert job.lease_expires_at is not None
        assert job.lease_expires_at > _NOW

        stored = await outbox_repository.get_job(projection_id)
        assert stored is not None
        assert stored.status is GraphProjectionJobStatus.RUNNING
        assert stored.attempt == 1
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_expired_lease_is_reclaimed_with_attempt_incremented(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    expired_lease = _NOW - timedelta(seconds=1)
    projection_id, *_ = await _insert_job(
        outbox_repository, status="running", attempt=1, lease_expires_at=expired_lease
    )
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        (job,) = [j for j in claimed if j.projection_id == projection_id]
        assert job.attempt == 2  # incremented on reclaim
        assert job.lease_expires_at is not None
        assert job.lease_expires_at > _NOW
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_a_live_lease_is_never_reclaimed(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    live_lease = _NOW + timedelta(seconds=300)
    projection_id, *_ = await _insert_job(
        outbox_repository, status="running", attempt=1, lease_expires_at=live_lease
    )
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        assert projection_id not in {j.projection_id for j in claimed}
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_retry_exhaustion_leaves_the_job_failed_and_inspectable(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    """Once attempts are exhausted, a retryable failure becomes durably `failed`, not requeued."""
    projection_id, *_ = await _insert_job(outbox_repository, max_attempts=1)
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        (job,) = [j for j in claimed if j.projection_id == projection_id]
        assert job.attempt == 1 == job.max_attempts

        await outbox_repository.mark_retryable_failure(
            projection_id,
            now=_NOW,
            error_code="graph_connection_error",
            error_message="a Neo4j read or write failed",
            requeue_status=GraphProjectionJobStatus.QUEUED,
        )

        stored = await outbox_repository.get_job(projection_id)
        assert stored is not None
        assert stored.status is GraphProjectionJobStatus.FAILED
        assert stored.last_error_code == "graph_connection_error"
        assert stored.completed_at is not None

        # A failed, attempts-exhausted job is never claimed again.
        reclaimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        assert projection_id not in {j.projection_id for j in reclaimed}
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_retryable_failure_with_attempts_remaining_requeues_not_fails(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    projection_id, *_ = await _insert_job(outbox_repository, max_attempts=5)
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        (job,) = [j for j in claimed if j.projection_id == projection_id]
        assert job.attempt == 1

        await outbox_repository.mark_retryable_failure(
            projection_id,
            now=_NOW,
            error_code="graph_connection_error",
            error_message="a Neo4j read or write failed",
            requeue_status=GraphProjectionJobStatus.QUEUED,
        )

        stored = await outbox_repository.get_job(projection_id)
        assert stored is not None
        assert stored.status is GraphProjectionJobStatus.QUEUED
        assert stored.completed_at is None

        # Requeued -- claimable again, and this time attempt becomes 2.
        reclaimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        (job2,) = [j for j in reclaimed if j.projection_id == projection_id]
        assert job2.attempt == 2
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_mark_succeeded_is_terminal_and_never_reclaimed(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    projection_id, *_ = await _insert_job(outbox_repository)
    try:
        await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        await outbox_repository.mark_succeeded(projection_id, _NOW)

        stored = await outbox_repository.get_job(projection_id)
        assert stored is not None
        assert stored.status is GraphProjectionJobStatus.SUCCEEDED
        assert stored.completed_at is not None
        assert stored.lease_expires_at is None

        reclaimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        assert projection_id not in {j.projection_id for j in reclaimed}
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_crash_recovery_sweep_marks_stuck_exhausted_job_failed(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    """A RUNNING row with an expired lease and no attempts left is swept to `failed`.

    Simulates a projector crashing mid-attempt on what was already its
    final allowed attempt: without the sweep, this row would sit in
    `running` forever -- never reclaimable (correctly) but also never
    terminal (a real "invisible" bug this test guards against).
    """
    expired_lease = _NOW - timedelta(seconds=1)
    projection_id, *_ = await _insert_job(
        outbox_repository,
        status="running",
        attempt=3,
        max_attempts=3,
        lease_expires_at=expired_lease,
    )
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        assert projection_id not in {j.projection_id for j in claimed}  # never reclaimed

        stored = await outbox_repository.get_job(projection_id)
        assert stored is not None
        assert stored.status is GraphProjectionJobStatus.FAILED  # swept, not left stuck
        assert stored.completed_at is not None
    finally:
        await _cleanup(outbox_repository, [projection_id])


async def test_deferred_status_is_also_claimable(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    projection_id, *_ = await _insert_job(outbox_repository, status="deferred", attempt=1)
    try:
        claimed = await outbox_repository.claim_batch(now=_NOW, lease_seconds=120, batch_size=10)
        assert projection_id in {j.projection_id for j in claimed}
    finally:
        await _cleanup(outbox_repository, [projection_id])
