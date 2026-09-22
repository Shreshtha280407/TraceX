"""Phase 6 Part 5 orchestration tests with fake repositories -- no live infra.

Covers: not-found/conflict propagation, exact-replay skips side effects, and
an integrity or Neo4j failure never blocks an already-committed decision
(mirrors `graph.intelligence.pipeline`'s established "safe, best-effort"
contract test style).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.hypothesis_models import (
    HypothesisActionKind,
    HypothesisActionRecord,
    HypothesisCreateSubmission,
    HypothesisRecord,
    HypothesisReviewSubmission,
    HypothesisStatus,
    text_commitment,
)
from app.modules.graph.integration_models import (
    CandidateLinkRecord,
    PropositionStatus,
)
from app.modules.graph.review_models import (
    CandidateReviewDecisionRecord,
    CandidateReviewOutcome,
    ReviewConflictError,
)
from app.modules.graph.review_service import (
    CandidateNotFoundError,
    create_hypothesis,
    submit_candidate_review_decision,
    submit_hypothesis_review_decision,
)

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


class _FakeGraph:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls = 0
        self._raise_error = raise_error

    async def write(self, query, parameters):
        self.calls += 1
        if self._raise_error:
            raise GraphConnectionError("graph write failed")
        return [{"ok": True}]


class _FakeIntegrityService:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.recorded = []
        self._raise_error = raise_error

    async def record_integrity_event(self, submission):
        self.recorded.append(submission)
        if self._raise_error:
            raise RuntimeError("integrity store unreachable")


class _FakeIntegrationRepository:
    def __init__(self, *, candidate=None, event=None) -> None:
        self._candidate = candidate
        self._event = event

    async def get_candidate_link(self, case_id, candidate_link_id):
        return self._candidate

    async def get_event_for_correlation(self, case_id, correlation_id):
        return self._event


class _FakeReviewProjectionOutbox:
    """Duck-typed stand-in for `ReviewProjectionOutboxRepository` -- records
    every call so tests can assert the durable-replay contract if needed."""

    def __init__(self) -> None:
        self.enqueued: list[tuple] = []
        self.succeeded: list = []
        self.retried: list = []

    async def enqueue(self, *, case_id, subject_type, subject_id, now, max_attempts=5):
        self.enqueued.append((case_id, subject_type, subject_id))

        class _Event:
            def __init__(self) -> None:
                self.event_id = uuid4()

        return _Event()

    async def mark_succeeded(self, event_id, now):
        self.succeeded.append(event_id)

    async def mark_retryable_failure(self, event_id, *, now, error_code, error_message):
        self.retried.append(event_id)


class _FakeReviewRepository:
    def __init__(self, *, result=None, raise_error: Exception | None = None) -> None:
        self._result = result
        self._raise_error = raise_error

    async def record_decision(self, **kwargs):
        if self._raise_error is not None:
            raise self._raise_error
        return self._result


class _Event:
    def __init__(self) -> None:
        self.projection_key = "synthetic-projection-key"


def _candidate(case_id) -> CandidateLinkRecord:
    return CandidateLinkRecord(
        candidate_link_id=uuid4(),
        correlation_id=uuid4(),
        case_id=case_id,
        idempotency_key="synthetic.candidate",
        left_observation_id=uuid4(),
        right_observation_id=uuid4(),
        status=PropositionStatus.NEEDS_REVIEW,
        reason_reference=None,
        evidence_paths=(),
        created_at=_NOW,
    )


def _decision(case_id, candidate_link_id) -> CandidateReviewDecisionRecord:
    return CandidateReviewDecisionRecord(
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


async def test_candidate_not_found_raises_before_any_write() -> None:
    with pytest.raises(CandidateNotFoundError):
        await submit_candidate_review_decision(
            case_id=uuid4(),
            candidate_link_id=uuid4(),
            submission=_review_submission(),
            reviewer_user_id=uuid4(),
            integration_repository=_FakeIntegrationRepository(candidate=None),
            review_repository=_FakeReviewRepository(),
            graph_repository=_FakeGraph(),
            integrity_service=_FakeIntegrityService(),
            review_projection_outbox=_FakeReviewProjectionOutbox(),
        )


def _review_submission():
    from app.modules.graph.review_models import CandidateReviewDecisionSubmission

    return CandidateReviewDecisionSubmission(decision=CandidateReviewOutcome.ACCEPTED_BY_REVIEWER)


async def test_conflict_error_propagates_unchanged() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    with pytest.raises(ReviewConflictError):
        await submit_candidate_review_decision(
            case_id=case_id,
            candidate_link_id=candidate.candidate_link_id,
            submission=_review_submission(),
            reviewer_user_id=uuid4(),
            integration_repository=_FakeIntegrationRepository(candidate=candidate),
            review_repository=_FakeReviewRepository(
                raise_error=ReviewConflictError(
                    "candidate already has a conflicting review decision"
                )
            ),
            graph_repository=_FakeGraph(),
            integrity_service=_FakeIntegrityService(),
            review_projection_outbox=_FakeReviewProjectionOutbox(),
        )


async def test_new_decision_fires_integrity_and_projection_exactly_once() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    integrity = _FakeIntegrityService()
    graph = _FakeGraph()
    view = await submit_candidate_review_decision(
        case_id=case_id,
        candidate_link_id=candidate.candidate_link_id,
        submission=_review_submission(),
        reviewer_user_id=uuid4(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=_Event()),
        review_repository=_FakeReviewRepository(result=(decision, True)),
        graph_repository=graph,
        integrity_service=integrity,
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert view.decision is decision
    assert len(integrity.recorded) == 1
    assert graph.calls == 1


async def test_replayed_decision_never_re_fires_side_effects() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    integrity = _FakeIntegrityService()
    graph = _FakeGraph()
    await submit_candidate_review_decision(
        case_id=case_id,
        candidate_link_id=candidate.candidate_link_id,
        submission=_review_submission(),
        reviewer_user_id=uuid4(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=_Event()),
        review_repository=_FakeReviewRepository(result=(decision, False)),
        graph_repository=graph,
        integrity_service=integrity,
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert integrity.recorded == []
    assert graph.calls == 0


async def test_integrity_failure_never_blocks_an_already_committed_decision() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    view = await submit_candidate_review_decision(
        case_id=case_id,
        candidate_link_id=candidate.candidate_link_id,
        submission=_review_submission(),
        reviewer_user_id=uuid4(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=_Event()),
        review_repository=_FakeReviewRepository(result=(decision, True)),
        graph_repository=_FakeGraph(),
        integrity_service=_FakeIntegrityService(raise_error=True),
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert view.decision is decision


async def test_graph_outage_never_blocks_an_already_committed_decision() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    view = await submit_candidate_review_decision(
        case_id=case_id,
        candidate_link_id=candidate.candidate_link_id,
        submission=_review_submission(),
        reviewer_user_id=uuid4(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=_Event()),
        review_repository=_FakeReviewRepository(result=(decision, True)),
        graph_repository=_FakeGraph(raise_error=True),
        integrity_service=_FakeIntegrityService(),
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert view.decision is decision


# --- Hypothesis orchestration -------------------------------------------------


class _FakeHypothesisRepository:
    def __init__(self, *, create_result=None, review_result="unset") -> None:
        self._create_result = create_result
        self._review_result = review_result

    async def create_hypothesis(self, **kwargs):
        return self._create_result

    async def record_review_decision(self, **kwargs):
        if self._review_result == "unset":
            raise AssertionError("record_review_decision not configured")
        return self._review_result


def _hypothesis(case_id, *, supporting_candidate_ids=()) -> HypothesisRecord:
    statement = "a synthetic hypothesis statement"
    return HypothesisRecord(
        hypothesis_id=uuid4(),
        case_id=case_id,
        status=HypothesisStatus.NEEDS_REVIEW,
        created_by=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        decided_at=None,
        decided_by=None,
        supporting_observation_ids=(),
        supporting_candidate_ids=supporting_candidate_ids,
        statement=statement,
        statement_commitment_sha256=text_commitment(statement),
        rationale=None,
        rationale_commitment_sha256=None,
    )


def _creation_action(hypothesis: HypothesisRecord) -> HypothesisActionRecord:
    return HypothesisActionRecord(
        hypothesis_action_id=uuid4(),
        case_id=hypothesis.case_id,
        hypothesis_id=hypothesis.hypothesis_id,
        action=HypothesisActionKind.CREATED,
        actor_user_id=hypothesis.created_by,
        rationale=None,
        rationale_commitment_sha256=None,
        created_at=_NOW,
        idempotency_key=hypothesis.creation_idempotency_key,
    )


async def test_new_hypothesis_fires_integrity_event_and_skips_projection_without_evidence() -> None:
    case_id = uuid4()
    candidate_id = uuid4()
    hypothesis = _hypothesis(case_id, supporting_candidate_ids=(candidate_id,))
    action = _creation_action(hypothesis)
    integrity = _FakeIntegrityService()
    graph = _FakeGraph()
    submission = HypothesisCreateSubmission(
        statement=hypothesis.statement, supporting_candidate_ids=(candidate_id,)
    )
    result = await create_hypothesis(
        case_id=case_id,
        submission=submission,
        created_by=hypothesis.created_by,
        hypothesis_repository=_FakeHypothesisRepository(create_result=(hypothesis, action, True)),
        integration_repository=_FakeIntegrationRepository(candidate=None),
        postgres_engine=object(),
        graph_repository=graph,
        integrity_service=integrity,
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert result is hypothesis
    assert len(integrity.recorded) == 1
    # No supporting_observation_ids -> project_hypothesis short-circuits before
    # touching the engine or the graph repository at all.
    assert graph.calls == 0


async def test_replayed_hypothesis_creation_never_re_fires_integrity() -> None:
    case_id = uuid4()
    hypothesis = _hypothesis(case_id)
    action = _creation_action(hypothesis)
    integrity = _FakeIntegrityService()
    submission = HypothesisCreateSubmission(
        statement=hypothesis.statement, supporting_observation_ids=(uuid4(),)
    )
    await create_hypothesis(
        case_id=case_id,
        submission=submission,
        created_by=hypothesis.created_by,
        hypothesis_repository=_FakeHypothesisRepository(create_result=(hypothesis, action, False)),
        integration_repository=_FakeIntegrationRepository(candidate=None),
        postgres_engine=object(),
        graph_repository=_FakeGraph(),
        integrity_service=integrity,
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert integrity.recorded == []


async def test_missing_hypothesis_returns_none_for_review() -> None:
    result = await submit_hypothesis_review_decision(
        case_id=uuid4(),
        hypothesis_id=uuid4(),
        submission=HypothesisReviewSubmission(decision="accepted_by_reviewer"),
        reviewer_user_id=uuid4(),
        hypothesis_repository=_FakeHypothesisRepository(review_result=None),
        graph_repository=_FakeGraph(),
        integrity_service=_FakeIntegrityService(),
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert result is None


async def test_hypothesis_review_decision_fires_integrity_once_for_a_new_decision() -> None:
    case_id = uuid4()
    hypothesis = _hypothesis(case_id).model_copy(
        update={
            "status": HypothesisStatus.ACCEPTED_BY_REVIEWER,
            "decided_at": _NOW,
            "decided_by": uuid4(),
        }
    )
    action = HypothesisActionRecord(
        hypothesis_action_id=uuid4(),
        case_id=case_id,
        hypothesis_id=hypothesis.hypothesis_id,
        action=HypothesisActionKind.ACCEPTED_BY_REVIEWER,
        actor_user_id=hypothesis.decided_by,
        rationale=None,
        rationale_commitment_sha256=None,
        created_at=_NOW,
        idempotency_key=hypothesis.review_idempotency_key,
    )
    integrity = _FakeIntegrityService()
    graph = _FakeGraph()
    result = await submit_hypothesis_review_decision(
        case_id=case_id,
        hypothesis_id=hypothesis.hypothesis_id,
        submission=HypothesisReviewSubmission(decision="accepted_by_reviewer"),
        reviewer_user_id=hypothesis.decided_by,
        hypothesis_repository=_FakeHypothesisRepository(review_result=(hypothesis, action, True)),
        graph_repository=graph,
        integrity_service=integrity,
        review_projection_outbox=_FakeReviewProjectionOutbox(),
    )
    assert result is hypothesis
    assert len(integrity.recorded) == 1
    assert graph.calls == 1
