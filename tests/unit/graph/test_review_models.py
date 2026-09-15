"""Phase 6 Part 5: pure-logic tests for candidate review-decision models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.graph.integration_models import (
    CandidateLinkRecord,
    PropositionStatus,
)
from app.modules.graph.review_models import (
    EFFECTIVE_STATUS_NEEDS_REVIEW,
    CandidateReviewDecisionRecord,
    CandidateReviewOutcome,
    candidate_review_view,
    rationale_commitment,
)

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _candidate(case_id, candidate_link_id) -> CandidateLinkRecord:
    return CandidateLinkRecord(
        candidate_link_id=candidate_link_id,
        correlation_id=uuid4(),
        case_id=case_id,
        idempotency_key="synthetic.candidate",
        left_observation_id=uuid4(),
        right_observation_id=uuid4(),
        status=PropositionStatus.NEEDS_REVIEW,
        reason_reference="exact_identifier",
        evidence_paths=(),
        created_at=_NOW,
    )


def _decision(case_id, candidate_link_id, *, decision=CandidateReviewOutcome.ACCEPTED_BY_REVIEWER):
    return CandidateReviewDecisionRecord(
        candidate_review_decision_id=uuid4(),
        case_id=case_id,
        candidate_link_id=candidate_link_id,
        correlation_id=uuid4(),
        decision=decision,
        reviewer_user_id=uuid4(),
        rationale="a plausible reviewer rationale",
        rationale_commitment_sha256=rationale_commitment("a plausible reviewer rationale"),
        created_at=_NOW,
    )


def test_rationale_commitment_is_none_for_no_rationale() -> None:
    assert rationale_commitment(None) is None


def test_rationale_commitment_is_a_deterministic_sha256() -> None:
    first = rationale_commitment("looks legitimate given the shared handle")
    second = rationale_commitment("looks legitimate given the shared handle")
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


def test_rationale_commitment_differs_for_different_text() -> None:
    assert rationale_commitment("one") != rationale_commitment("two")


def test_candidate_review_view_without_a_decision_is_needs_review() -> None:
    case_id, candidate_link_id = uuid4(), uuid4()
    candidate = _candidate(case_id, candidate_link_id)
    view = candidate_review_view(candidate, None)
    assert view.review_status == EFFECTIVE_STATUS_NEEDS_REVIEW
    assert view.decision is None
    assert view.candidate == candidate


def test_candidate_review_view_with_a_decision_reflects_the_decision() -> None:
    case_id, candidate_link_id = uuid4(), uuid4()
    candidate = _candidate(case_id, candidate_link_id)
    decision = _decision(
        case_id, candidate_link_id, decision=CandidateReviewOutcome.REJECTED_BY_REVIEWER
    )
    view = candidate_review_view(candidate, decision)
    assert view.review_status == "rejected_by_reviewer"
    assert view.decision is decision


def test_decision_idempotency_key_is_scoped_to_case_and_candidate_only() -> None:
    case_id, candidate_link_id = uuid4(), uuid4()
    accepted = _decision(
        case_id, candidate_link_id, decision=CandidateReviewOutcome.ACCEPTED_BY_REVIEWER
    )
    rejected = _decision(
        case_id, candidate_link_id, decision=CandidateReviewOutcome.REJECTED_BY_REVIEWER
    )
    # One decision per candidate: the key never distinguishes by outcome/reviewer,
    # so any second attempt with different content is a conflict, not a new row.
    expected = f"candidate-review:{case_id}:{candidate_link_id}"
    assert accepted.idempotency_key == rejected.idempotency_key == expected


def test_integrity_submission_never_carries_raw_rationale_text() -> None:
    case_id, candidate_link_id = uuid4(), uuid4()
    decision = _decision(case_id, candidate_link_id)
    submission = decision.to_integrity_submission()
    serialized = str(submission.canonical_metadata)
    assert "a plausible reviewer rationale" not in serialized
    assert submission.canonical_metadata["rationale_provided"] is True
    assert (
        submission.canonical_metadata["rationale_commitment_sha256"]
        == decision.rationale_commitment_sha256
    )
    assert submission.subject_id == str(decision.candidate_review_decision_id)
    assert submission.idempotency_key == decision.idempotency_key


def test_integrity_submission_rationale_provided_is_false_when_none_given() -> None:
    case_id, candidate_link_id = uuid4(), uuid4()
    decision = CandidateReviewDecisionRecord(
        candidate_review_decision_id=uuid4(),
        case_id=case_id,
        candidate_link_id=candidate_link_id,
        correlation_id=uuid4(),
        decision=CandidateReviewOutcome.ACCEPTED_BY_REVIEWER,
        reviewer_user_id=uuid4(),
        rationale=None,
        rationale_commitment_sha256=None,
        created_at=_NOW,
    )
    submission = decision.to_integrity_submission()
    assert submission.canonical_metadata["rationale_provided"] is False
    assert submission.canonical_metadata["rationale_commitment_sha256"] is None
