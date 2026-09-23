"""In-memory duck-typed stand-in for `EntityRepository` -- mirrors
`tests/fixtures/access_control/fake_repository.py`'s pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.contracts.entity import EntityV1
from app.core.pagination import CursorPosition
from app.modules.graph.entity_models import (
    EntityResolutionCandidateRecord,
    EntityReviewDecisionRecord,
    EntityReviewOutcome,
    rationale_commitment,
)


class FakeEntityRepository:
    def __init__(self) -> None:
        self.entities: dict[UUID, EntityV1] = {}
        self._entity_by_observation: dict[tuple[UUID, UUID], UUID] = {}
        self.candidates: dict[UUID, EntityResolutionCandidateRecord] = {}
        self.decisions: list[EntityReviewDecisionRecord] = []

    async def close(self) -> None:
        pass

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
        key = (case_id, source_observation_id)
        existing_id = self._entity_by_observation.get(key)
        if existing_id is not None:
            return self.entities[existing_id], False
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
        self.entities[entity_id] = entity
        self._entity_by_observation[key] = entity_id
        return entity, True

    async def get_entity_by_id_any_case(self, entity_id: UUID) -> EntityV1 | None:
        return self.entities.get(entity_id)

    async def get_entity(self, case_id: UUID, entity_id: UUID) -> EntityV1 | None:
        entity = self.entities.get(entity_id)
        return entity if entity is not None and entity.case_id == case_id else None

    async def list_entities_by_observation(
        self, case_id: UUID, observation_ids: tuple[UUID, ...]
    ) -> dict[UUID, EntityV1]:
        return {
            obs_id: self.entities[entity_id]
            for (c_id, obs_id), entity_id in self._entity_by_observation.items()
            if c_id == case_id and obs_id in observation_ids
        }

    async def list_entities(
        self, case_id: UUID, *, limit: int | None = None, after: CursorPosition | None = None
    ) -> list[EntityV1]:
        items = sorted(
            (e for e in self.entities.values() if e.case_id == case_id),
            key=lambda e: (e.created_at, e.entity_id),
            reverse=True,
        )
        if after is not None:
            items = [
                e for e in items if (e.created_at, e.entity_id) < (after.created_at, after.row_id)
            ]
        return items[:limit] if limit is not None else items

    async def upsert_candidate(
        self, candidate: EntityResolutionCandidateRecord
    ) -> tuple[EntityResolutionCandidateRecord, bool]:
        for existing in self.candidates.values():
            if (
                existing.case_id == candidate.case_id
                and existing.left_entity_id == candidate.left_entity_id
                and existing.right_entity_id == candidate.right_entity_id
                and existing.config_version == candidate.config_version
            ):
                return existing, False
        self.candidates[candidate.entity_resolution_candidate_id] = candidate
        return candidate, True

    async def get_candidate(
        self, case_id: UUID, entity_resolution_candidate_id: UUID
    ) -> EntityResolutionCandidateRecord | None:
        candidate = self.candidates.get(entity_resolution_candidate_id)
        return candidate if candidate is not None and candidate.case_id == case_id else None

    async def list_candidates(
        self, case_id: UUID, *, limit: int | None = None
    ) -> list[EntityResolutionCandidateRecord]:
        items = sorted(
            (c for c in self.candidates.values() if c.case_id == case_id),
            key=lambda c: c.created_at,
        )
        return items[:limit] if limit is not None else items

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
        record = EntityReviewDecisionRecord(
            entity_review_decision_id=decision_id,
            case_id=case_id,
            entity_resolution_candidate_id=entity_resolution_candidate_id,
            decision=decision,
            reviewer_user_id=reviewer_user_id,
            rationale=rationale,
            rationale_commitment_sha256=rationale_commitment(rationale),
            created_at=now or datetime.now(UTC),
        )
        self.decisions.append(record)
        return record

    async def list_decisions(
        self, case_id: UUID, entity_resolution_candidate_id: UUID
    ) -> list[EntityReviewDecisionRecord]:
        return sorted(
            (
                d
                for d in self.decisions
                if d.case_id == case_id
                and d.entity_resolution_candidate_id == entity_resolution_candidate_id
            ),
            key=lambda d: d.created_at,
        )
