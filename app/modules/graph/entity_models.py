"""Gap-Closure WP-2: case-scoped entities, resolution candidates, and review decisions.

`EntityV1` (`app/contracts/entity.py`, frozen) is instantiated here for the
first time in this codebase. An entity is created from exactly one
observation (this WP's documented scope -- see `entity_service.py`'s
module docstring) and is never auto-merged with another. Two entities that
share identity signal (found via the existing `graph.intelligence.
retrieval` cascade, unchanged) become an `EntityResolutionCandidateRecord`
-- a reviewable proposition, never a merge. A human reviewer's decision
(`EntityReviewDecisionRecord`) is the only path that can treat two
entities as the same identity, and it is reversible: unlike
`candidate_review_decisions` (one immutable decision per candidate,
Phase 6 Part 5's own precedent), a candidate pair here may accumulate
more than one decision over time (`verified_same` then later `split`) --
the *effective* state is always the most recent decision, computed at
read time, exactly like `review_models.candidate_review_view` already
computes effective status from "does a decision row exist yet."
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.contracts.entity import EntityV1
from app.core.canonical import canonical_sha256
from app.modules.graph.intelligence.models import RetrievalReason
from app.modules.graph.models import GraphModel
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission

ENTITY_RESOLUTION_REVIEW_SCHEMA_VERSION = "entity_resolution_review.v1"
ENTITY_RESOLUTION_DECISION_SCHEMA_VERSION = "entity_resolution_decision.v1"
ENTITY_CANDIDATE_CONFIG_VERSION = "entity_resolution_cascade_v1"
MAX_RATIONALE_LENGTH = 4_000


class EntityReviewOutcome(StrEnum):
    """A human reviewer's judgement on one entity-resolution candidate.

    `VERIFIED_SAME` is the one authorized path that may treat two entities
    as one identity -- never automatic, never inferred from name/alias
    similarity alone. `SPLIT` reverses an earlier `VERIFIED_SAME` for the
    same candidate pair (a new, additive decision -- the old one is never
    deleted or edited).
    """

    VERIFIED_SAME = "verified_same"
    REJECTED = "rejected"
    SPLIT = "split"


EFFECTIVE_STATUS_NEEDS_REVIEW = "needs_review"


class EntityResolutionCandidateRecord(GraphModel):
    """A durable `entity_resolution_candidates` row: a reviewable proposition only."""

    entity_resolution_candidate_id: UUID
    case_id: UUID
    left_entity_id: UUID
    right_entity_id: UUID
    reasons: tuple[RetrievalReason, ...]
    identifier_types: tuple[str, ...] = ()
    vector_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    contradiction_reasons: tuple[str, ...] = ()
    supporting_observation_ids: tuple[UUID, ...]
    config_version: str
    created_at: datetime


class EntityReviewDecisionSubmission(GraphModel):
    """The protected, case-scoped write boundary for one human review decision."""

    decision: EntityReviewOutcome
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_LENGTH)


class EntityReviewDecisionRecord(GraphModel):
    """A full, immutable `entity_review_decisions` row.

    `rationale` never leaves the protected database read path -- every
    other surface (integrity event, structured logs) carries only
    `rationale_commitment_sha256`, mirroring
    `review_models.CandidateReviewDecisionRecord` exactly.
    """

    entity_review_decision_id: UUID
    case_id: UUID
    entity_resolution_candidate_id: UUID
    decision: EntityReviewOutcome
    reviewer_user_id: UUID
    rationale: str | None
    rationale_commitment_sha256: str | None
    created_at: datetime

    @property
    def idempotency_key(self) -> str:
        """Unlike `candidate_review_decisions`, a candidate pair may accumulate
        more than one decision -- keyed on this decision's own ID, not the
        candidate's, so a later `split` never collides with an earlier
        `verified_same` for the same pair."""
        return f"entity-resolution-decision:{self.case_id}:{self.entity_review_decision_id}"

    def to_integrity_submission(self) -> IntegrityEventSubmission:
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.ENTITY_RESOLUTION_DECISION,
            subject_type="entity_review_decision",
            subject_id=str(self.entity_review_decision_id),
            canonical_metadata={
                "entity_review_decision_id": str(self.entity_review_decision_id),
                "entity_resolution_candidate_id": str(self.entity_resolution_candidate_id),
                "decision": self.decision.value,
                "reviewer_user_id": str(self.reviewer_user_id),
                "rationale_provided": self.rationale is not None,
                "rationale_commitment_sha256": self.rationale_commitment_sha256,
            },
            payload_schema_version=ENTITY_RESOLUTION_DECISION_SCHEMA_VERSION,
            source_created_at=self.created_at,
            idempotency_key=self.idempotency_key,
        )


def rationale_commitment(rationale: str | None) -> str | None:
    return canonical_sha256({"rationale": rationale}) if rationale is not None else None


class EntityResolutionReviewView(GraphModel):
    """Safe read shape: one candidate plus its effective (most-recent-decision) state."""

    candidate: EntityResolutionCandidateRecord
    effective_status: str
    latest_decision: EntityReviewDecisionRecord | None
    decision_history: tuple[EntityReviewDecisionRecord, ...] = ()


def entity_resolution_review_view(
    candidate: EntityResolutionCandidateRecord,
    decisions: tuple[EntityReviewDecisionRecord, ...],
) -> EntityResolutionReviewView:
    """`decisions` must already be sorted oldest-first; the latest one is authoritative."""
    latest = decisions[-1] if decisions else None
    effective_status = (
        latest.decision.value if latest is not None else EFFECTIVE_STATUS_NEEDS_REVIEW
    )
    return EntityResolutionReviewView(
        candidate=candidate,
        effective_status=effective_status,
        latest_decision=latest,
        decision_history=decisions,
    )


class EntityResolutionCandidateListResponse(GraphModel):
    items: tuple[EntityResolutionReviewView, ...]


class EntityView(GraphModel):
    """Safe, case-scoped read shape for one entity."""

    entity: EntityV1


__all__ = [
    "ENTITY_CANDIDATE_CONFIG_VERSION",
    "ENTITY_RESOLUTION_DECISION_SCHEMA_VERSION",
    "ENTITY_RESOLUTION_REVIEW_SCHEMA_VERSION",
    "EFFECTIVE_STATUS_NEEDS_REVIEW",
    "EntityResolutionCandidateListResponse",
    "EntityResolutionCandidateRecord",
    "EntityResolutionReviewView",
    "EntityReviewDecisionRecord",
    "EntityReviewDecisionSubmission",
    "EntityReviewOutcome",
    "EntityView",
    "entity_resolution_review_view",
    "rationale_commitment",
]
