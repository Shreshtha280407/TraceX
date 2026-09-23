"""PostgreSQL durable boundary for Phase 6 Part 5 evidence-backed hypotheses.

Two tables, mirroring the split Nipun's own correlation seam already uses
between a mutable proposition (`correlation_records`) and its durable event
log: `hypotheses` is a normal mutable row (status/decided_at/decided_by/
updated_at change exactly once, on the one allowed review decision);
`hypothesis_actions` is an append-only audit trail (one row per creation or
review decision, protected by the same PostgreSQL trigger Nipun's integrity
tables use) that is also exactly what `IntegrityEventSubmission`s are built
from, live or during reconciliation.

Referenced observations/candidates are re-read from their own canonical,
case-scoped tables before a hypothesis is created -- never trusted from the
request -- exactly like `GraphCorrelationIntegrationRepository.submit()`
re-reads `worker_observations` before accepting a correlation submission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.core.ids import deterministic_uuid
from app.core.pagination import CursorPosition
from app.modules.evidence_lifecycle.repository import worker_observations_table
from app.modules.graph.entity_repository import entity_resolution_candidates_table
from app.modules.graph.hypothesis_models import (
    HypothesisActionKind,
    HypothesisActionRecord,
    HypothesisConflictError,
    HypothesisRecord,
    HypothesisReviewOutcome,
    HypothesisStatus,
    HypothesisValidationError,
    text_commitment,
)
from app.modules.graph.integration_repository import candidate_links_table

metadata = sa.MetaData()

hypotheses_table = sa.Table(
    "hypotheses",
    metadata,
    sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("supporting_observation_ids", postgresql.JSONB(), nullable=False),
    sa.Column("supporting_candidate_ids", postgresql.JSONB(), nullable=False),
    #: ADR-031, additive migration -- see module docstring for why this is
    #: a second, parallel citation column, never merged with the one above.
    sa.Column("supporting_entity_resolution_candidate_ids", postgresql.JSONB(), nullable=False),
    sa.Column("statement", sa.Text(), nullable=False),
    sa.Column("statement_commitment_sha256", sa.Text(), nullable=False),
    sa.Column("rationale", sa.Text(), nullable=True),
    sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
)

hypothesis_actions_table = sa.Table(
    "hypothesis_actions",
    metadata,
    sa.Column("hypothesis_action_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("action", sa.Text(), nullable=False),
    sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("rationale", sa.Text(), nullable=True),
    sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _hypothesis(row: sa.RowMapping | dict[str, object]) -> HypothesisRecord:
    values = dict(row)
    values["supporting_observation_ids"] = tuple(
        UUID(v) for v in values["supporting_observation_ids"]
    )
    values["supporting_candidate_ids"] = tuple(UUID(v) for v in values["supporting_candidate_ids"])
    values["supporting_entity_resolution_candidate_ids"] = tuple(
        UUID(v) for v in values["supporting_entity_resolution_candidate_ids"]
    )
    return HypothesisRecord.model_validate(values)


def _action(row: sa.RowMapping | dict[str, object]) -> HypothesisActionRecord:
    return HypothesisActionRecord.model_validate(dict(row))


class HypothesisRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_hypothesis(
        self,
        *,
        case_id: UUID,
        statement: str,
        rationale: str | None,
        created_by: UUID,
        supporting_observation_ids: tuple[UUID, ...],
        supporting_candidate_ids: tuple[UUID, ...],
        supporting_entity_resolution_candidate_ids: tuple[UUID, ...] = (),
        now: datetime | None = None,
    ) -> tuple[HypothesisRecord, HypothesisActionRecord, bool]:
        """Persist one hypothesis and its creation action atomically.

        Returns `(hypothesis, creation_action, is_new)`. An exact repeat of
        the same case/author/statement/rationale/references maps to the same
        deterministic ID and replays; every reference is re-verified against
        its own canonical, case-scoped table before any write.

        `supporting_entity_resolution_candidate_ids` (ADR-031) is a second,
        parallel reference list verified against `entity_resolution_
        candidates_table` -- never merged with `supporting_candidate_ids`,
        which stays verified against `candidate_links_table` exactly as
        before.
        """
        now = now or datetime.now(UTC)
        statement_commitment = text_commitment(statement)
        rationale_commitment = text_commitment(rationale)
        hypothesis_id = deterministic_uuid(
            "phase_6_hypothesis",
            str(case_id),
            str(created_by),
            statement_commitment or "",
            rationale_commitment or "",
            ",".join(sorted(str(v) for v in supporting_observation_ids)),
            ",".join(sorted(str(v) for v in supporting_candidate_ids)),
            ",".join(sorted(str(v) for v in supporting_entity_resolution_candidate_ids)),
        )
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(hypotheses_table).where(
                            hypotheses_table.c.case_id == case_id,
                            hypotheses_table.c.hypothesis_id == hypothesis_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                record = _hypothesis(existing)
                action_row = (
                    (
                        await conn.execute(
                            sa.select(hypothesis_actions_table).where(
                                hypothesis_actions_table.c.case_id == case_id,
                                hypothesis_actions_table.c.idempotency_key
                                == record.creation_idempotency_key,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return record, _action(action_row), False

            if supporting_observation_ids:
                found = (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(worker_observations_table)
                        .where(
                            worker_observations_table.c.case_id == case_id,
                            worker_observations_table.c.observation_id.in_(
                                supporting_observation_ids
                            ),
                        )
                    )
                ).scalar_one()
                if found != len(set(supporting_observation_ids)):
                    raise HypothesisValidationError(
                        "one or more supporting observations are missing or belong to a"
                        " different case"
                    )
            if supporting_candidate_ids:
                found = (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(candidate_links_table)
                        .where(
                            candidate_links_table.c.case_id == case_id,
                            candidate_links_table.c.candidate_link_id.in_(supporting_candidate_ids),
                        )
                    )
                ).scalar_one()
                if found != len(set(supporting_candidate_ids)):
                    raise HypothesisValidationError(
                        "one or more supporting candidates are missing or belong to a"
                        " different case"
                    )
            if supporting_entity_resolution_candidate_ids:
                found = (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(entity_resolution_candidates_table)
                        .where(
                            entity_resolution_candidates_table.c.case_id == case_id,
                            entity_resolution_candidates_table.c.entity_resolution_candidate_id.in_(
                                supporting_entity_resolution_candidate_ids
                            ),
                        )
                    )
                ).scalar_one()
                if found != len(set(supporting_entity_resolution_candidate_ids)):
                    raise HypothesisValidationError(
                        "one or more supporting entity-resolution candidates are missing or"
                        " belong to a different case"
                    )

            record = HypothesisRecord(
                hypothesis_id=hypothesis_id,
                case_id=case_id,
                status=HypothesisStatus.NEEDS_REVIEW,
                created_by=created_by,
                created_at=now,
                updated_at=now,
                decided_at=None,
                decided_by=None,
                supporting_observation_ids=supporting_observation_ids,
                supporting_candidate_ids=supporting_candidate_ids,
                supporting_entity_resolution_candidate_ids=supporting_entity_resolution_candidate_ids,
                statement=statement,
                statement_commitment_sha256=statement_commitment or "",
                rationale=rationale,
                rationale_commitment_sha256=rationale_commitment,
            )
            await conn.execute(
                sa.insert(hypotheses_table).values(
                    hypothesis_id=record.hypothesis_id,
                    case_id=record.case_id,
                    status=record.status.value,
                    created_by=record.created_by,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    decided_at=record.decided_at,
                    decided_by=record.decided_by,
                    supporting_observation_ids=[str(v) for v in record.supporting_observation_ids],
                    supporting_candidate_ids=[str(v) for v in record.supporting_candidate_ids],
                    supporting_entity_resolution_candidate_ids=[
                        str(v) for v in record.supporting_entity_resolution_candidate_ids
                    ],
                    statement=record.statement,
                    statement_commitment_sha256=record.statement_commitment_sha256,
                    rationale=record.rationale,
                    rationale_commitment_sha256=record.rationale_commitment_sha256,
                )
            )
            action = HypothesisActionRecord(
                hypothesis_action_id=deterministic_uuid(
                    "phase_6_hypothesis_action", str(case_id), str(hypothesis_id), "created"
                ),
                case_id=case_id,
                hypothesis_id=hypothesis_id,
                action=HypothesisActionKind.CREATED,
                actor_user_id=created_by,
                rationale=None,
                rationale_commitment_sha256=None,
                created_at=now,
                idempotency_key=record.creation_idempotency_key,
            )
            await conn.execute(
                sa.insert(hypothesis_actions_table).values(
                    hypothesis_action_id=action.hypothesis_action_id,
                    case_id=action.case_id,
                    hypothesis_id=action.hypothesis_id,
                    action=action.action.value,
                    actor_user_id=action.actor_user_id,
                    rationale=action.rationale,
                    rationale_commitment_sha256=action.rationale_commitment_sha256,
                    created_at=action.created_at,
                    idempotency_key=action.idempotency_key,
                )
            )
            return record, action, True

    async def record_review_decision(
        self,
        *,
        case_id: UUID,
        hypothesis_id: UUID,
        decision: HypothesisReviewOutcome,
        reviewer_user_id: UUID,
        rationale: str | None,
        now: datetime | None = None,
    ) -> tuple[HypothesisRecord, HypothesisActionRecord, bool] | None:
        """Persist one review decision atomically. Returns `None` if the hypothesis
        does not exist for this case. Returns `(hypothesis, action, is_new)`
        otherwise -- `is_new=False` for an exact replay of the same decision."""
        now = now or datetime.now(UTC)
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(hypotheses_table).where(
                            hypotheses_table.c.case_id == case_id,
                            hypotheses_table.c.hypothesis_id == hypothesis_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            current = _hypothesis(row)

            existing_action_row = (
                (
                    await conn.execute(
                        sa.select(hypothesis_actions_table).where(
                            hypothesis_actions_table.c.case_id == case_id,
                            hypothesis_actions_table.c.idempotency_key
                            == current.review_idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing_action_row is not None:
                existing_action = _action(existing_action_row)
                if (
                    existing_action.action.value != decision.value
                    or existing_action.actor_user_id != reviewer_user_id
                    or existing_action.rationale != rationale
                ):
                    raise HypothesisConflictError(
                        "hypothesis already has a conflicting review decision"
                    )
                return current, existing_action, False

            if current.status != HypothesisStatus.NEEDS_REVIEW:
                raise HypothesisConflictError(
                    "hypothesis is not awaiting review"
                )  # pragma: no cover - defensive; status/action rows stay in lockstep

            await conn.execute(
                sa.update(hypotheses_table)
                .where(
                    hypotheses_table.c.case_id == case_id,
                    hypotheses_table.c.hypothesis_id == hypothesis_id,
                )
                .values(
                    status=decision.value,
                    decided_at=now,
                    decided_by=reviewer_user_id,
                    updated_at=now,
                )
            )
            updated = current.model_copy(
                update={
                    "status": HypothesisStatus(decision.value),
                    "decided_at": now,
                    "decided_by": reviewer_user_id,
                    "updated_at": now,
                }
            )
            action = HypothesisActionRecord(
                hypothesis_action_id=deterministic_uuid(
                    "phase_6_hypothesis_action", str(case_id), str(hypothesis_id), "reviewed"
                ),
                case_id=case_id,
                hypothesis_id=hypothesis_id,
                action=HypothesisActionKind(decision.value),
                actor_user_id=reviewer_user_id,
                rationale=rationale,
                rationale_commitment_sha256=text_commitment(rationale),
                created_at=now,
                idempotency_key=current.review_idempotency_key,
            )
            await conn.execute(
                sa.insert(hypothesis_actions_table).values(
                    hypothesis_action_id=action.hypothesis_action_id,
                    case_id=action.case_id,
                    hypothesis_id=action.hypothesis_id,
                    action=action.action.value,
                    actor_user_id=action.actor_user_id,
                    rationale=action.rationale,
                    rationale_commitment_sha256=action.rationale_commitment_sha256,
                    created_at=action.created_at,
                    idempotency_key=action.idempotency_key,
                )
            )
            return updated, action, True

    async def get_hypothesis(self, case_id: UUID, hypothesis_id: UUID) -> HypothesisRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(hypotheses_table).where(
                            hypotheses_table.c.case_id == case_id,
                            hypotheses_table.c.hypothesis_id == hypothesis_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _hypothesis(row) if row else None

    async def list_hypotheses(
        self,
        case_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
        after: CursorPosition | None = None,
    ) -> list[HypothesisRecord]:
        """Gap-Closure WP-6/re-close (G7/G17 pagination): `after` (an opaque,
        case-bound, tamper-evident cursor -- see `core.pagination`) is real
        keyset pagination and takes precedence over `offset` when given;
        `offset` remains for a caller that hasn't adopted cursors yet. Rows
        are ordered `(created_at DESC, hypothesis_id DESC)` -- a composite,
        fully deterministic order, required for keyset pagination to never
        skip or repeat a row even when several share the same `created_at`.
        """
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("hypothesis query limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("hypothesis query offset must be >= 0")
        statement = (
            sa.select(hypotheses_table)
            .where(hypotheses_table.c.case_id == case_id)
            .order_by(hypotheses_table.c.created_at.desc(), hypotheses_table.c.hypothesis_id.desc())
        )
        if after is not None:
            statement = statement.where(
                sa.or_(
                    hypotheses_table.c.created_at < after.created_at,
                    sa.and_(
                        hypotheses_table.c.created_at == after.created_at,
                        hypotheses_table.c.hypothesis_id < after.row_id,
                    ),
                )
            )
        else:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [_hypothesis(row) for row in rows]

    async def list_actions(self, case_id: UUID) -> list[HypothesisActionRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(hypothesis_actions_table)
                        .where(hypothesis_actions_table.c.case_id == case_id)
                        .order_by(hypothesis_actions_table.c.created_at.asc())
                    )
                )
                .mappings()
                .all()
            )
        return [_action(row) for row in rows]
