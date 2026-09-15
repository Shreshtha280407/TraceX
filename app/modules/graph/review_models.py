"""Phase 6 Part 5: case-scoped human review-decision models for correlation candidates.

A review decision is a human's explicit accept/reject judgement on one of
Nipun's already-persisted, already-scored `CandidateLinkRecord` rows. It
never mutates that row (`correlation_candidate_links.status` stays exactly
what Nipun's Phase 5 scoring pipeline produced) -- it is a separate,
additive, append-only fact layered on top, exactly like an
`IntegrityEvent` is layered on top of the primary write it seals. The
*effective* review status a caller sees (`needs_review` /
`accepted_by_reviewer` / `rejected_by_reviewer`) is computed at read time
from "does a decision row exist yet", never stored as a mutation of the
candidate itself.

`rationale` is human-authored, potentially sensitive case narrative: it is
stored raw only in this protected table (read back only through the
protected, case-scoped API this module adds). Every other surface --
the `review_decision` integrity event, the Neo4j projection, structured
logs, and reconciliation -- carries only `rationale_commitment_sha256`
(or nothing at all when no rationale was given), never the raw text. See
`docs/architecture/phase-6-review-and-hypothesis.md`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from app.core.canonical import canonical_sha256
from app.modules.graph.integration_models import CandidateLinkRecord
from app.modules.graph.models import GraphModel
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission

REVIEW_DECISION_SCHEMA_VERSION = "review_decision.v1"
MAX_RATIONALE_LENGTH = 4_000


class CandidateReviewOutcome(StrEnum):
    """A human reviewer's terminal judgement on one candidate link.

    Deliberately only two values: a review decision is never itself a
    verified identity, a merged entity, or a guilt conclusion -- it only
    records whether a human accepted or rejected the *candidate
    correlation* for further use (e.g. inclusion in a hypothesis).
    """

    ACCEPTED_BY_REVIEWER = "accepted_by_reviewer"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"


#: The effective status a caller sees for review purposes: either a real
#: `CandidateReviewOutcome` (a decision was recorded) or this sentinel (none
#: yet). Matches the task's minimum required lifecycle
#: `needs_review -> accepted_by_reviewer | rejected_by_reviewer` without
#: overloading Nipun's own `PropositionStatus` values.
EFFECTIVE_STATUS_NEEDS_REVIEW = "needs_review"


class CandidateReviewDecisionSubmission(GraphModel):
    """The protected, case-scoped write boundary for one human review decision."""

    decision: CandidateReviewOutcome
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE_LENGTH)


class CandidateReviewDecisionRecord(GraphModel):
    """A full, immutable `candidate_review_decisions` row.

    `rationale` is protected-database-only: never included in
    `to_integrity_submission`'s metadata, a Neo4j projection property, or a
    structured log line -- only `rationale_commitment_sha256` is.
    """

    candidate_review_decision_id: UUID
    case_id: UUID
    candidate_link_id: UUID
    correlation_id: UUID
    decision: CandidateReviewOutcome
    reviewer_user_id: UUID
    rationale: str | None
    rationale_commitment_sha256: str | None
    created_at: datetime

    @property
    def idempotency_key(self) -> str:
        """One decision per candidate -- see `review_repository.py`'s conflict rule."""
        return f"candidate-review:{self.case_id}:{self.candidate_link_id}"

    def to_integrity_submission(self) -> IntegrityEventSubmission:
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.REVIEW_DECISION,
            subject_type="candidate_review_decision",
            subject_id=str(self.candidate_review_decision_id),
            canonical_metadata={
                "candidate_review_decision_id": str(self.candidate_review_decision_id),
                "candidate_link_id": str(self.candidate_link_id),
                "correlation_id": str(self.correlation_id),
                "decision": self.decision.value,
                "reviewer_user_id": str(self.reviewer_user_id),
                "rationale_provided": self.rationale is not None,
                "rationale_commitment_sha256": self.rationale_commitment_sha256,
            },
            payload_schema_version=REVIEW_DECISION_SCHEMA_VERSION,
            source_created_at=self.created_at,
            idempotency_key=self.idempotency_key,
        )


def rationale_commitment(rationale: str | None) -> str | None:
    """A safe SHA-256 commitment for an optional rationale -- never the text itself."""
    return canonical_sha256({"rationale": rationale}) if rationale is not None else None


class CandidateReviewView(GraphModel):
    """Safe, protected-API read shape: one candidate plus its effective review state.

    `review_status` is always exactly one of `needs_review`,
    `accepted_by_reviewer`, or `rejected_by_reviewer` -- computed from
    whether `decision` is present, never a stored mutation of `candidate`.
    """

    candidate: CandidateLinkRecord
    review_status: str
    decision: CandidateReviewDecisionRecord | None


def candidate_review_view(
    candidate: CandidateLinkRecord, decision: CandidateReviewDecisionRecord | None
) -> CandidateReviewView:
    review_status = (
        decision.decision.value if decision is not None else EFFECTIVE_STATUS_NEEDS_REVIEW
    )
    return CandidateReviewView(
        candidate=candidate,
        review_status=review_status,
        decision=decision,
    )


class CandidateReviewListResponse(GraphModel):
    items: tuple[CandidateReviewView, ...]


class ReviewConflictError(ValueError):
    """A candidate already has a decision that conflicts with this request."""


__all__ = [
    "REVIEW_DECISION_SCHEMA_VERSION",
    "CandidateReviewDecisionRecord",
    "CandidateReviewDecisionSubmission",
    "CandidateReviewListResponse",
    "CandidateReviewOutcome",
    "CandidateReviewView",
    "ReviewConflictError",
    "candidate_review_view",
    "rationale_commitment",
]
