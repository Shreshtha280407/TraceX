"""PostgreSQL durable boundary for Phase 6 Part 5 candidate review decisions.

Mirrors `app.modules.integrity.repository`'s idempotent-record /
fingerprint-comparison pattern: an exact retry (same case, candidate,
decision, reviewer, rationale) replays the existing row; anything else
under the same `(case_id, candidate_link_id)` conflicts and is rejected
before any write -- a candidate never gets a second, overwriting decision.
`candidate_review_decisions` rows are append-only (see the migration's
PostgreSQL trigger, reusing the same `tracex_reject_integrity_record_mutation`
function Nipun's integrity tables use): there is no update or delete method
here at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.core.ids import deterministic_uuid
from app.modules.graph.review_models import (
    CandidateReviewDecisionRecord,
    CandidateReviewOutcome,
    ReviewConflictError,
    rationale_commitment,
)

metadata = sa.MetaData()

candidate_review_decisions_table = sa.Table(
    "candidate_review_decisions",
    metadata,
    sa.Column("candidate_review_decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("candidate_link_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("decision", sa.Text(), nullable=False),
    sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("rationale", sa.Text(), nullable=True),
    sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _record(row: sa.RowMapping | dict[str, object]) -> CandidateReviewDecisionRecord:
    return CandidateReviewDecisionRecord.model_validate(dict(row))


class CandidateReviewRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def record_decision(
        self,
        *,
        case_id: UUID,
        candidate_link_id: UUID,
        correlation_id: UUID,
        decision: CandidateReviewOutcome,
        reviewer_user_id: UUID,
        rationale: str | None,
        now: datetime | None = None,
    ) -> tuple[CandidateReviewDecisionRecord, bool]:
        """Persist one decision. Returns `(record, is_new)`.

        `is_new` is `False` for an exact replay -- callers use it to decide
        whether to fire the `review_decision` integrity event and Neo4j
        projection again (they must not, for a pure replay).
        """
        now = now or datetime.now(UTC)
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(candidate_review_decisions_table).where(
                            candidate_review_decisions_table.c.case_id == case_id,
                            candidate_review_decisions_table.c.candidate_link_id
                            == candidate_link_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                record = _record(existing)
                if (
                    record.decision != decision
                    or record.reviewer_user_id != reviewer_user_id
                    or record.rationale != rationale
                ):
                    raise ReviewConflictError("candidate already has a conflicting review decision")
                return record, False

            record = CandidateReviewDecisionRecord(
                candidate_review_decision_id=deterministic_uuid(
                    "phase_6_candidate_review_decision", str(case_id), str(candidate_link_id)
                ),
                case_id=case_id,
                candidate_link_id=candidate_link_id,
                correlation_id=correlation_id,
                decision=decision,
                reviewer_user_id=reviewer_user_id,
                rationale=rationale,
                rationale_commitment_sha256=rationale_commitment(rationale),
                created_at=now,
            )
            await conn.execute(
                sa.insert(candidate_review_decisions_table).values(
                    candidate_review_decision_id=record.candidate_review_decision_id,
                    case_id=record.case_id,
                    candidate_link_id=record.candidate_link_id,
                    correlation_id=record.correlation_id,
                    decision=record.decision.value,
                    reviewer_user_id=record.reviewer_user_id,
                    rationale=record.rationale,
                    rationale_commitment_sha256=record.rationale_commitment_sha256,
                    created_at=record.created_at,
                )
            )
            return record, True

    async def get_decision(
        self, case_id: UUID, candidate_link_id: UUID
    ) -> CandidateReviewDecisionRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(candidate_review_decisions_table).where(
                            candidate_review_decisions_table.c.case_id == case_id,
                            candidate_review_decisions_table.c.candidate_link_id
                            == candidate_link_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _record(row) if row else None

    async def list_decisions(self, case_id: UUID) -> list[CandidateReviewDecisionRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(candidate_review_decisions_table)
                        .where(candidate_review_decisions_table.c.case_id == case_id)
                        .order_by(candidate_review_decisions_table.c.created_at.asc())
                    )
                )
                .mappings()
                .all()
            )
        return [_record(row) for row in rows]
