"""Phase 6 Part 5: pure-logic tests for evidence-backed hypothesis models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.graph.hypothesis_models import (
    HypothesisActionKind,
    HypothesisActionRecord,
    HypothesisCreateSubmission,
    HypothesisRecord,
    HypothesisStatus,
    text_commitment,
)

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _hypothesis(
    case_id, hypothesis_id, *, statement="a synthetic hypothesis statement"
) -> HypothesisRecord:
    return HypothesisRecord(
        hypothesis_id=hypothesis_id,
        case_id=case_id,
        status=HypothesisStatus.NEEDS_REVIEW,
        created_by=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        decided_at=None,
        decided_by=None,
        supporting_observation_ids=(uuid4(),),
        supporting_candidate_ids=(),
        supporting_entity_resolution_candidate_ids=(),
        statement=statement,
        statement_commitment_sha256=text_commitment(statement),
        rationale="a synthetic supporting rationale",
        rationale_commitment_sha256=text_commitment("a synthetic supporting rationale"),
    )


def test_text_commitment_is_none_for_none() -> None:
    assert text_commitment(None) is None


def test_text_commitment_is_deterministic_sha256() -> None:
    a = text_commitment("the two numbers called each other repeatedly")
    b = text_commitment("the two numbers called each other repeatedly")
    assert a == b
    assert len(a) == 64


def test_create_submission_requires_at_least_one_reference() -> None:
    with pytest.raises(ValidationError):
        HypothesisCreateSubmission(statement="no references at all")


def test_create_submission_accepts_observation_only_reference() -> None:
    submission = HypothesisCreateSubmission(
        statement="cites one observation", supporting_observation_ids=(uuid4(),)
    )
    assert len(submission.supporting_observation_ids) == 1


def test_create_submission_accepts_candidate_only_reference() -> None:
    submission = HypothesisCreateSubmission(
        statement="cites one candidate", supporting_candidate_ids=(uuid4(),)
    )
    assert len(submission.supporting_candidate_ids) == 1


def test_create_submission_rejects_duplicate_observation_refs() -> None:
    observation_id = uuid4()
    with pytest.raises(ValidationError):
        HypothesisCreateSubmission(
            statement="duplicate refs", supporting_observation_ids=(observation_id, observation_id)
        )


def test_create_submission_rejects_duplicate_candidate_refs() -> None:
    candidate_id = uuid4()
    with pytest.raises(ValidationError):
        HypothesisCreateSubmission(
            statement="duplicate refs", supporting_candidate_ids=(candidate_id, candidate_id)
        )


def test_create_submission_accepts_entity_resolution_candidate_only_reference() -> None:
    """ADR-031: a hypothesis can cite a WP-2 entity-resolution candidate --
    a second, parallel citation path, distinct from `supporting_candidate_
    ids` (which stays scoped to Phase 5 correlation candidates)."""
    submission = HypothesisCreateSubmission(
        statement="cites one entity-resolution candidate",
        supporting_entity_resolution_candidate_ids=(uuid4(),),
    )
    assert len(submission.supporting_entity_resolution_candidate_ids) == 1
    assert submission.supporting_candidate_ids == ()


def test_create_submission_rejects_duplicate_entity_resolution_candidate_refs() -> None:
    candidate_id = uuid4()
    with pytest.raises(ValidationError):
        HypothesisCreateSubmission(
            statement="duplicate refs",
            supporting_entity_resolution_candidate_ids=(candidate_id, candidate_id),
        )


def test_statement_and_rationale_are_bounded() -> None:
    with pytest.raises(ValidationError):
        HypothesisCreateSubmission(statement="x" * 5000, supporting_observation_ids=(uuid4(),))


def test_safe_metadata_never_carries_raw_statement_or_rationale() -> None:
    hypothesis = _hypothesis(uuid4(), uuid4())
    metadata = hypothesis.safe_metadata()
    serialized = str(metadata)
    assert hypothesis.statement not in serialized
    assert hypothesis.rationale not in serialized
    assert metadata["statement_commitment_sha256"] == hypothesis.statement_commitment_sha256
    assert metadata["rationale_commitment_sha256"] == hypothesis.rationale_commitment_sha256


def test_safe_metadata_carries_entity_resolution_candidate_ids() -> None:
    """ADR-031: the new citation list flows through `safe_metadata()` the
    same as the pre-existing two, so it reaches the integrity event and
    Neo4j projection surfaces identically."""
    candidate_id = uuid4()
    hypothesis = _hypothesis(uuid4(), uuid4()).model_copy(
        update={"supporting_entity_resolution_candidate_ids": (candidate_id,)}
    )
    metadata = hypothesis.safe_metadata()
    assert metadata["supporting_entity_resolution_candidate_ids"] == [str(candidate_id)]


def test_hypothesis_action_integrity_submission_never_carries_raw_text() -> None:
    case_id, hypothesis_id = uuid4(), uuid4()
    hypothesis = _hypothesis(case_id, hypothesis_id)
    action = HypothesisActionRecord(
        hypothesis_action_id=uuid4(),
        case_id=case_id,
        hypothesis_id=hypothesis_id,
        action=HypothesisActionKind.CREATED,
        actor_user_id=hypothesis.created_by,
        rationale=None,
        rationale_commitment_sha256=None,
        created_at=_NOW,
        idempotency_key=hypothesis.creation_idempotency_key,
    )
    submission = action.to_integrity_submission(hypothesis=hypothesis)
    serialized = str(submission.canonical_metadata)
    assert hypothesis.statement not in serialized
    assert hypothesis.rationale not in serialized
    assert submission.subject_id == str(hypothesis_id)
    assert submission.idempotency_key == hypothesis.creation_idempotency_key


def test_hypothesis_status_never_contains_a_confirmed_or_true_value() -> None:
    values = {member.value for member in HypothesisStatus}
    assert values == {"needs_review", "accepted_by_reviewer", "rejected_by_reviewer"}


def test_review_idempotency_key_is_fixed_regardless_of_outcome() -> None:
    hypothesis_accepted = _hypothesis(uuid4(), uuid4())
    same_ids_hypothesis = hypothesis_accepted.model_copy(
        update={"status": HypothesisStatus.REJECTED_BY_REVIEWER}
    )
    assert hypothesis_accepted.review_idempotency_key == same_ids_hypothesis.review_idempotency_key
