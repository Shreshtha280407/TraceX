"""Gap-Closure WP-5: pure-logic tests for `EntityReviewDecisionRecord.to_integrity_submission`.

Mirrors `test_review_models.py::test_integrity_submission_never_carries_raw_rationale_text`
exactly -- same never-the-raw-content guarantee, applied to entity resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.graph.entity_models import (
    ENTITY_RESOLUTION_DECISION_SCHEMA_VERSION,
    EntityReviewDecisionRecord,
    EntityReviewOutcome,
    rationale_commitment,
)
from app.modules.integrity.models import IntegrityEventKind

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _decision(
    *, rationale: str | None = "same handle across two observations"
) -> EntityReviewDecisionRecord:
    return EntityReviewDecisionRecord(
        entity_review_decision_id=uuid4(),
        case_id=uuid4(),
        entity_resolution_candidate_id=uuid4(),
        decision=EntityReviewOutcome.VERIFIED_SAME,
        reviewer_user_id=uuid4(),
        rationale=rationale,
        rationale_commitment_sha256=rationale_commitment(rationale),
        created_at=_NOW,
    )


def test_integrity_submission_never_carries_raw_rationale_text() -> None:
    decision = _decision()
    submission = decision.to_integrity_submission()
    serialized = submission.model_dump_json()
    assert "same handle across two observations" not in serialized
    assert submission.event_kind == IntegrityEventKind.ENTITY_RESOLUTION_DECISION
    assert submission.payload_schema_version == ENTITY_RESOLUTION_DECISION_SCHEMA_VERSION
    assert submission.canonical_metadata["rationale_provided"] is True
    assert (
        submission.canonical_metadata["rationale_commitment_sha256"]
        == decision.rationale_commitment_sha256
    )


def test_integrity_submission_rationale_provided_is_false_when_none_given() -> None:
    decision = _decision(rationale=None)
    submission = decision.to_integrity_submission()
    assert submission.canonical_metadata["rationale_provided"] is False
    assert submission.canonical_metadata["rationale_commitment_sha256"] is None


def test_idempotency_key_is_scoped_to_the_decision_not_the_candidate() -> None:
    """Unlike `candidate_review_decisions` (one decision per candidate), a
    candidate pair here may accumulate multiple decisions (`verified_same`
    then `split`) -- each must get its own integrity idempotency key."""
    first = _decision()
    second = _decision()
    assert first.idempotency_key != second.idempotency_key
    assert str(first.entity_review_decision_id) in first.idempotency_key
