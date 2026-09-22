"""Gap-Closure WP-4 (G13): durable replay outbox for review/hypothesis
Neo4j projection.

`review_service.py`'s projection calls were previously best-effort only --
a Neo4j outage silently dropped the projection forever (a real, pre-existing,
self-documented gap: see that module's own docstring). This repository adds
exactly what's needed to fix it: enqueue a durable event before attempting
the immediate projection, mark it succeeded on success, and leave it queued
on failure for `replay_once`/`replay_loop` to retry later -- mirroring
`outbox_repository.GraphProjectionOutboxRepository`'s claim/lease/retry
shape, reusing the same `GraphProjectionJobStatus` values, but against its
own, separate `review_hypothesis_projection_events` table (see that
migration's docstring for why a new table, not a shared one).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.modules.graph.models import (
    CLAIMABLE_PROJECTION_JOB_STATUSES,
    GraphModel,
    GraphProjectionJobStatus,
)


class ReviewProjectionSubjectType(StrEnum):
    CANDIDATE_REVIEW_DECISION = "candidate_review_decision"
    HYPOTHESIS_CREATED = "hypothesis_created"
    HYPOTHESIS_REVIEW_DECISION = "hypothesis_review_decision"


class ReviewProjectionEventRecord(GraphModel):
    event_id: UUID
    case_id: UUID
    subject_type: ReviewProjectionSubjectType
    subject_id: UUID
    status: GraphProjectionJobStatus
    attempt: int
    max_attempts: int
    lease_expires_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


metadata = sa.MetaData()

review_hypothesis_projection_events_table = sa.Table(
    "review_hypothesis_projection_events",
    metadata,
    sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("subject_type", sa.Text(), nullable=False),
    sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("max_attempts", sa.Integer(), nullable=False),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error_code", sa.Text(), nullable=True),
    sa.Column("last_error_message", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _record(row: sa.RowMapping) -> ReviewProjectionEventRecord:
    return ReviewProjectionEventRecord.model_validate(dict(row))


class ReviewProjectionOutboxRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def enqueue(
        self,
        *,
        case_id: UUID,
        subject_type: ReviewProjectionSubjectType,
        subject_id: UUID,
        now: datetime,
        max_attempts: int = 5,
    ) -> ReviewProjectionEventRecord:
        """Idempotent by `(case_id, subject_type, subject_id)`: a replayed
        enqueue (e.g. a retried API call) returns the existing row unchanged."""
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(review_hypothesis_projection_events_table).where(
                            review_hypothesis_projection_events_table.c.case_id == case_id,
                            review_hypothesis_projection_events_table.c.subject_type
                            == subject_type.value,
                            review_hypothesis_projection_events_table.c.subject_id == subject_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return _record(existing)

            record = ReviewProjectionEventRecord(
                event_id=uuid4(),
                case_id=case_id,
                subject_type=subject_type,
                subject_id=subject_id,
                status=GraphProjectionJobStatus.QUEUED,
                attempt=0,
                max_attempts=max_attempts,
                lease_expires_at=None,
                last_error_code=None,
                last_error_message=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            await conn.execute(
                sa.insert(review_hypothesis_projection_events_table).values(
                    event_id=record.event_id,
                    case_id=record.case_id,
                    subject_type=record.subject_type.value,
                    subject_id=record.subject_id,
                    status=record.status.value,
                    attempt=record.attempt,
                    max_attempts=record.max_attempts,
                    lease_expires_at=None,
                    last_error_code=None,
                    last_error_message=None,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    completed_at=None,
                )
            )
            return record

    async def mark_succeeded(self, event_id: UUID, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(review_hypothesis_projection_events_table)
                .where(review_hypothesis_projection_events_table.c.event_id == event_id)
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
        self, event_id: UUID, *, now: datetime, error_code: str, error_message: str
    ) -> None:
        """Requeue (if attempts remain) or leave durably `FAILED`. `error_message`
        must already be sanitized by the caller -- never a raw driver exception."""
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(
                            review_hypothesis_projection_events_table.c.attempt,
                            review_hypothesis_projection_events_table.c.max_attempts,
                        )
                        .where(review_hypothesis_projection_events_table.c.event_id == event_id)
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
                sa.update(review_hypothesis_projection_events_table)
                .where(review_hypothesis_projection_events_table.c.event_id == event_id)
                .values(
                    status=(
                        GraphProjectionJobStatus.FAILED.value
                        if exhausted
                        else GraphProjectionJobStatus.QUEUED.value
                    ),
                    lease_expires_at=None,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    updated_at=now,
                    completed_at=now if exhausted else None,
                )
            )

    async def claim_batch(
        self, *, now: datetime, lease_seconds: int, batch_size: int
    ) -> list[ReviewProjectionEventRecord]:
        """Same crash-recovery-sweep-then-claim shape as
        `outbox_repository.GraphProjectionOutboxRepository.claim_batch`."""
        claimable_statuses = tuple(s.value for s in CLAIMABLE_PROJECTION_JOB_STATUSES)
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(review_hypothesis_projection_events_table)
                .where(
                    review_hypothesis_projection_events_table.c.status
                    == GraphProjectionJobStatus.RUNNING.value,
                    review_hypothesis_projection_events_table.c.lease_expires_at < now,
                    review_hypothesis_projection_events_table.c.attempt
                    >= review_hypothesis_projection_events_table.c.max_attempts,
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
                sa.select(review_hypothesis_projection_events_table)
                .where(
                    sa.or_(
                        review_hypothesis_projection_events_table.c.status.in_(claimable_statuses),
                        sa.and_(
                            review_hypothesis_projection_events_table.c.status
                            == GraphProjectionJobStatus.RUNNING.value,
                            review_hypothesis_projection_events_table.c.lease_expires_at < now,
                        ),
                    ),
                    review_hypothesis_projection_events_table.c.attempt
                    < review_hypothesis_projection_events_table.c.max_attempts,
                )
                .order_by(review_hypothesis_projection_events_table.c.created_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await conn.execute(select_stmt)).mappings().all()
            if not rows:
                return []

            lease_expires_at = now + timedelta(seconds=lease_seconds)
            claimed: list[ReviewProjectionEventRecord] = []
            for row in rows:
                new_attempt = row["attempt"] + 1
                await conn.execute(
                    sa.update(review_hypothesis_projection_events_table)
                    .where(review_hypothesis_projection_events_table.c.event_id == row["event_id"])
                    .values(
                        status=GraphProjectionJobStatus.RUNNING.value,
                        attempt=new_attempt,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                )
                claimed.append(
                    ReviewProjectionEventRecord(
                        event_id=row["event_id"],
                        case_id=row["case_id"],
                        subject_type=ReviewProjectionSubjectType(row["subject_type"]),
                        subject_id=row["subject_id"],
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


__all__ = [
    "ReviewProjectionEventRecord",
    "ReviewProjectionOutboxRepository",
    "ReviewProjectionSubjectType",
    "create_engine",
]
