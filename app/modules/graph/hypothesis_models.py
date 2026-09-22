"""Phase 6 Part 5: human-created, evidence-backed, reviewable hypotheses.

A hypothesis is never automatically generated: it exists only because an
authorized case member wrote a bounded statement and cited at least one
case-scoped observation or reviewed candidate. It is always an explicitly
reviewable inference -- `HypothesisStatus` never contains a "confirmed" or
"true" value, only `needs_review` / `accepted_by_reviewer` /
`rejected_by_reviewer` -- and it never creates a timeless entity-to-entity
relationship: its only durable references are to observations/candidates,
never to a fabricated `Entity`-to-`Entity` edge.

`statement`/`rationale` are human-authored, potentially sensitive case
narrative. They are persisted raw only in the protected, case-scoped
`hypotheses`/`hypothesis_actions` tables (readable back only through this
module's protected API). Every other surface -- the `hypothesis_action`
integrity event, the Neo4j projection, structured logs, and reconciliation
-- carries only `*_commitment_sha256`, via `HypothesisRecord.safe_metadata`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.core.canonical import canonical_sha256
from app.modules.graph.models import GraphModel
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission

HYPOTHESIS_ACTION_SCHEMA_VERSION = "hypothesis_action.v1"
MAX_STATEMENT_LENGTH = 4_000
MAX_RATIONALE_LENGTH = 4_000


class HypothesisStatus(StrEnum):
    """Never a value that reads as a confirmed fact -- see module docstring."""

    NEEDS_REVIEW = "needs_review"
    ACCEPTED_BY_REVIEWER = "accepted_by_reviewer"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"


class HypothesisReviewOutcome(StrEnum):
    ACCEPTED_BY_REVIEWER = "accepted_by_reviewer"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"


class HypothesisActionKind(StrEnum):
    """One `hypothesis_actions` row per durable creation or review decision."""

    CREATED = "created"
    ACCEPTED_BY_REVIEWER = "accepted_by_reviewer"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"


def text_commitment(value: str | None) -> str | None:
    """A safe SHA-256 commitment for optional human-authored text."""
    return canonical_sha256({"text": value}) if value is not None else None


class HypothesisCreateSubmission(GraphModel):
    """The protected, case-scoped write boundary for proposing one hypothesis."""

    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_LENGTH)
    supporting_observation_ids: tuple[UUID, ...] = ()
    supporting_candidate_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def _require_at_least_one_reference(self) -> HypothesisCreateSubmission:
        if not self.supporting_observation_ids and not self.supporting_candidate_ids:
            raise ValueError(
                "a hypothesis must cite at least one case-scoped observation or candidate"
            )
        if len(set(self.supporting_observation_ids)) != len(self.supporting_observation_ids):
            raise ValueError("supporting observation references must be unique")
        if len(set(self.supporting_candidate_ids)) != len(self.supporting_candidate_ids):
            raise ValueError("supporting candidate references must be unique")
        return self


class HypothesisReviewSubmission(GraphModel):
    decision: HypothesisReviewOutcome
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_LENGTH)


class HypothesisRecord(GraphModel):
    """A full `hypotheses` row. Mutable: `status`/`decided_at`/`decided_by`/
    `updated_at` change exactly once, on the one allowed review decision --
    everything else is fixed at creation."""

    hypothesis_id: UUID
    case_id: UUID
    status: HypothesisStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None
    decided_by: UUID | None
    supporting_observation_ids: tuple[UUID, ...]
    supporting_candidate_ids: tuple[UUID, ...]
    statement: str
    statement_commitment_sha256: str
    rationale: str | None
    rationale_commitment_sha256: str | None

    @property
    def creation_idempotency_key(self) -> str:
        return f"hypothesis-action:{self.case_id}:{self.hypothesis_id}:created"

    @property
    def review_idempotency_key(self) -> str:
        """One fixed key regardless of outcome: a hypothesis is reviewed at most once."""
        return f"hypothesis-action:{self.case_id}:{self.hypothesis_id}:reviewed"

    def safe_metadata(self) -> dict[str, Any]:
        """The exact safe projection used by the integrity leaf and Neo4j -- never raw text."""
        return {
            "hypothesis_id": str(self.hypothesis_id),
            "case_id": str(self.case_id),
            "status": self.status.value,
            "created_by": str(self.created_by),
            "supporting_observation_ids": [str(v) for v in self.supporting_observation_ids],
            "supporting_candidate_ids": [str(v) for v in self.supporting_candidate_ids],
            "statement_commitment_sha256": self.statement_commitment_sha256,
            "rationale_commitment_sha256": self.rationale_commitment_sha256,
        }


class HypothesisActionRecord(GraphModel):
    """A full, immutable `hypothesis_actions` row -- append-only audit trail."""

    hypothesis_action_id: UUID
    case_id: UUID
    hypothesis_id: UUID
    action: HypothesisActionKind
    actor_user_id: UUID
    rationale: str | None
    rationale_commitment_sha256: str | None
    created_at: datetime
    idempotency_key: str

    def to_integrity_submission(self, *, hypothesis: HypothesisRecord) -> IntegrityEventSubmission:
        metadata = hypothesis.safe_metadata()
        metadata.update(
            {
                "hypothesis_action_id": str(self.hypothesis_action_id),
                "action": self.action.value,
                "actor_user_id": str(self.actor_user_id),
                "action_rationale_commitment_sha256": self.rationale_commitment_sha256,
            }
        )
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.HYPOTHESIS_ACTION,
            subject_type="hypothesis",
            subject_id=str(self.hypothesis_id),
            canonical_metadata=metadata,
            payload_schema_version=HYPOTHESIS_ACTION_SCHEMA_VERSION,
            source_created_at=self.created_at,
            idempotency_key=self.idempotency_key,
        )


class HypothesisListResponse(GraphModel):
    items: tuple[HypothesisRecord, ...]
    #: Gap-Closure re-close (G17): an opaque, case-bound, tamper-evident
    #: cursor (see `core.pagination`) a caller passes back as `?cursor=...`
    #: to fetch the next page. `None` when this page was empty -- there is
    #: nothing to resume from.
    next_cursor: str | None = None


class HypothesisValidationError(ValueError):
    """A referenced observation/candidate is missing, cross-case, or otherwise invalid."""


class HypothesisConflictError(ValueError):
    """A hypothesis already has a review decision that conflicts with this request."""


class HypothesisNotFoundError(ValueError):
    """No hypothesis exists for this case/ID."""


__all__ = [
    "HYPOTHESIS_ACTION_SCHEMA_VERSION",
    "HypothesisActionKind",
    "HypothesisActionRecord",
    "HypothesisConflictError",
    "HypothesisCreateSubmission",
    "HypothesisListResponse",
    "HypothesisNotFoundError",
    "HypothesisRecord",
    "HypothesisReviewOutcome",
    "HypothesisReviewSubmission",
    "HypothesisStatus",
    "HypothesisValidationError",
    "text_commitment",
]
