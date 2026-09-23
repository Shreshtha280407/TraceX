"""PostgreSQL durable boundary for the entity layer (Gap-Closure WP-2).

Mirrors `review_repository.py`'s structure. One real difference:
`entity_review_decisions` allows more than one decision per candidate over
time (a reversible merge), so `record_decision` always appends rather than
replaying/conflicting on an existing row -- see `entity_models.py`'s
module docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.contracts.entity import EntityV1
from app.core.config import Settings
from app.core.pagination import CursorPosition
from app.modules.graph.entity_models import (
    EntityResolutionCandidateRecord,
    EntityReviewDecisionRecord,
    EntityReviewOutcome,
    rationale_commitment,
)

metadata = sa.MetaData()

entities_table = sa.Table(
    "entities",
    metadata,
    sa.Column("entity_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("entity_type", sa.Text(), nullable=False),
    sa.Column("canonical_label", sa.Text(), nullable=False),
    sa.Column("aliases", postgresql.JSONB(), nullable=False),
    sa.Column("stable_identifiers", postgresql.JSONB(), nullable=False),
    sa.Column("attributes", postgresql.JSONB(), nullable=False),
    sa.Column("created_from_observation_ids", postgresql.JSONB(), nullable=False),
    sa.Column("review_status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("source_observation_id", postgresql.UUID(as_uuid=True), nullable=False),
)

entity_resolution_candidates_table = sa.Table(
    "entity_resolution_candidates",
    metadata,
    sa.Column("entity_resolution_candidate_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("left_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("right_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("reasons", postgresql.JSONB(), nullable=False),
    sa.Column("identifier_types", postgresql.JSONB(), nullable=False),
    sa.Column("vector_score", sa.Float(), nullable=True),
    sa.Column("contradiction_reasons", postgresql.JSONB(), nullable=False),
    sa.Column("supporting_observation_ids", postgresql.JSONB(), nullable=False),
    sa.Column("config_version", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

entity_review_decisions_table = sa.Table(
    "entity_review_decisions",
    metadata,
    sa.Column("entity_review_decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("entity_resolution_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("decision", sa.Text(), nullable=False),
    sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("rationale", sa.Text(), nullable=True),
    sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _entity(row: sa.RowMapping) -> EntityV1:
    data = dict(row)
    data.pop("source_observation_id", None)
    return EntityV1.model_validate(data)


def _candidate(row: sa.RowMapping) -> EntityResolutionCandidateRecord:
    return EntityResolutionCandidateRecord.model_validate(dict(row))


def _decision(row: sa.RowMapping) -> EntityReviewDecisionRecord:
    return EntityReviewDecisionRecord.model_validate(dict(row))


class EntityRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    # --- entities ----------------------------------------------------------

    async def get_or_create_entity_for_observation(
        self,
        *,
        entity_id: UUID,
        case_id: UUID,
        source_observation_id: UUID,
        entity_type: str,
        canonical_label: str,
        aliases: tuple[str, ...],
        stable_identifiers: dict[str, str],
        created_at: datetime,
    ) -> tuple[EntityV1, bool]:
        """Idempotent by `(case_id, source_observation_id)`. Returns `(entity, is_new)`.

        Never merges: a second call for the same observation returns the
        existing row unchanged, and two different observations always get
        two different entities, even with identical identifiers -- that
        similarity is exactly what `entity_resolution_candidates` exists
        to surface for review, never to auto-resolve here.
        """
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(entities_table).where(
                            entities_table.c.case_id == case_id,
                            entities_table.c.source_observation_id == source_observation_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return _entity(existing), False

            entity = EntityV1(
                entity_id=entity_id,
                case_id=case_id,
                entity_type=entity_type,
                canonical_label=canonical_label,
                aliases=list(aliases),
                stable_identifiers=dict(stable_identifiers),
                attributes={},
                created_from_observation_ids=[source_observation_id],
                created_at=created_at,
            )
            await conn.execute(
                sa.insert(entities_table).values(
                    entity_id=entity.entity_id,
                    case_id=entity.case_id,
                    entity_type=entity.entity_type,
                    canonical_label=entity.canonical_label,
                    aliases=entity.aliases,
                    stable_identifiers=entity.stable_identifiers,
                    attributes=entity.attributes,
                    created_from_observation_ids=[
                        str(v) for v in entity.created_from_observation_ids
                    ],
                    review_status=entity.review_status.value,
                    created_at=entity.created_at,
                    source_observation_id=source_observation_id,
                )
            )
            return entity, True

    async def get_entity_by_id_any_case(self, entity_id: UUID) -> EntityV1 | None:
        """Cross-case lookup by ID alone -- callers MUST still authorize the
        returned `case_id` before returning any content (see `entity_api.py`'s
        `_authorize_entity_action`). Used only because the entity-scoped
        routes have no `case_id` path parameter to authorize against first.
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(entities_table).where(entities_table.c.entity_id == entity_id)
                    )
                )
                .mappings()
                .first()
            )
        return _entity(row) if row is not None else None

    async def get_entity(self, case_id: UUID, entity_id: UUID) -> EntityV1 | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(entities_table).where(
                            entities_table.c.case_id == case_id,
                            entities_table.c.entity_id == entity_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _entity(row) if row is not None else None

    async def list_entities_by_observation(
        self, case_id: UUID, observation_ids: tuple[UUID, ...]
    ) -> dict[UUID, EntityV1]:
        """`{source_observation_id: entity}` for every entity already created from these."""
        if not observation_ids:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(entities_table).where(
                            entities_table.c.case_id == case_id,
                            entities_table.c.source_observation_id.in_(observation_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {row["source_observation_id"]: _entity(row) for row in rows}

    async def list_entities(
        self, case_id: UUID, *, limit: int | None = None, after: CursorPosition | None = None
    ) -> list[EntityV1]:
        """Case-scoped entity listing, real keyset pagination (mirrors
        `HypothesisRepository.list_hypotheses`'s pattern exactly -- see
        `app.core.pagination`). Rows are ordered `(created_at DESC,
        entity_id DESC)`, a composite, fully deterministic order required
        for keyset pagination to never skip or repeat a row even when
        several entities share the same `created_at`.
        """
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("entity query limit must be between 1 and 200")
        statement = (
            sa.select(entities_table)
            .where(entities_table.c.case_id == case_id)
            .order_by(entities_table.c.created_at.desc(), entities_table.c.entity_id.desc())
        )
        if after is not None:
            statement = statement.where(
                sa.or_(
                    entities_table.c.created_at < after.created_at,
                    sa.and_(
                        entities_table.c.created_at == after.created_at,
                        entities_table.c.entity_id < after.row_id,
                    ),
                )
            )
        if limit is not None:
            statement = statement.limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [_entity(row) for row in rows]

    # --- resolution candidates ----------------------------------------------

    async def upsert_candidate(
        self, candidate: EntityResolutionCandidateRecord
    ) -> tuple[EntityResolutionCandidateRecord, bool]:
        """Idempotent by `(case_id, left_entity_id, right_entity_id, config_version)`."""
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(entity_resolution_candidates_table).where(
                            entity_resolution_candidates_table.c.case_id == candidate.case_id,
                            entity_resolution_candidates_table.c.left_entity_id
                            == candidate.left_entity_id,
                            entity_resolution_candidates_table.c.right_entity_id
                            == candidate.right_entity_id,
                            entity_resolution_candidates_table.c.config_version
                            == candidate.config_version,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return _candidate(existing), False

            await conn.execute(
                sa.insert(entity_resolution_candidates_table).values(
                    entity_resolution_candidate_id=candidate.entity_resolution_candidate_id,
                    case_id=candidate.case_id,
                    left_entity_id=candidate.left_entity_id,
                    right_entity_id=candidate.right_entity_id,
                    reasons=[r.value for r in candidate.reasons],
                    identifier_types=list(candidate.identifier_types),
                    vector_score=candidate.vector_score,
                    contradiction_reasons=list(candidate.contradiction_reasons),
                    supporting_observation_ids=[
                        str(v) for v in candidate.supporting_observation_ids
                    ],
                    config_version=candidate.config_version,
                    created_at=candidate.created_at,
                )
            )
            return candidate, True

    async def get_candidate(
        self, case_id: UUID, entity_resolution_candidate_id: UUID
    ) -> EntityResolutionCandidateRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(entity_resolution_candidates_table).where(
                            entity_resolution_candidates_table.c.case_id == case_id,
                            entity_resolution_candidates_table.c.entity_resolution_candidate_id
                            == entity_resolution_candidate_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _candidate(row) if row is not None else None

    async def list_candidates(
        self, case_id: UUID, *, limit: int | None = None
    ) -> list[EntityResolutionCandidateRecord]:
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("entity resolution candidate query limit must be between 1 and 200")
        statement = (
            sa.select(entity_resolution_candidates_table)
            .where(entity_resolution_candidates_table.c.case_id == case_id)
            .order_by(entity_resolution_candidates_table.c.created_at.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [_candidate(row) for row in rows]

    # --- review decisions (append-only; more than one per candidate allowed) ----

    async def record_decision(
        self,
        *,
        case_id: UUID,
        entity_resolution_candidate_id: UUID,
        decision: EntityReviewOutcome,
        reviewer_user_id: UUID,
        rationale: str | None,
        decision_id: UUID,
        now: datetime | None = None,
    ) -> EntityReviewDecisionRecord:
        now = now or datetime.now(UTC)
        record = EntityReviewDecisionRecord(
            entity_review_decision_id=decision_id,
            case_id=case_id,
            entity_resolution_candidate_id=entity_resolution_candidate_id,
            decision=decision,
            reviewer_user_id=reviewer_user_id,
            rationale=rationale,
            rationale_commitment_sha256=rationale_commitment(rationale),
            created_at=now,
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(entity_review_decisions_table).values(
                    entity_review_decision_id=record.entity_review_decision_id,
                    case_id=record.case_id,
                    entity_resolution_candidate_id=record.entity_resolution_candidate_id,
                    decision=record.decision.value,
                    reviewer_user_id=record.reviewer_user_id,
                    rationale=record.rationale,
                    rationale_commitment_sha256=record.rationale_commitment_sha256,
                    created_at=record.created_at,
                )
            )
        return record

    async def list_decisions(
        self, case_id: UUID, entity_resolution_candidate_id: UUID
    ) -> list[EntityReviewDecisionRecord]:
        """Oldest first -- the caller treats the last one as authoritative."""
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(entity_review_decisions_table)
                        .where(
                            entity_review_decisions_table.c.case_id == case_id,
                            entity_review_decisions_table.c.entity_resolution_candidate_id
                            == entity_resolution_candidate_id,
                        )
                        .order_by(entity_review_decisions_table.c.created_at.asc())
                    )
                )
                .mappings()
                .all()
            )
        return [_decision(row) for row in rows]


__all__ = ["EntityRepository", "create_engine"]
