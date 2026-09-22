"""Gap-Closure WP-4 (G13): review/hypothesis projection replay dispatch logic.

Fakes every repository dependency -- no live infra. Mirrors
`test_review_service.py`'s fake-repository style.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.hypothesis_models import HypothesisRecord, HypothesisStatus
from app.modules.graph.integration_models import CandidateLinkRecord, PropositionStatus
from app.modules.graph.review_models import CandidateReviewDecisionRecord, CandidateReviewOutcome
from app.modules.graph.review_projection_outbox import GraphProjectionJobStatus as _Status
from app.modules.graph.review_projection_outbox import (
    ReviewProjectionEventRecord,
    ReviewProjectionSubjectType,
)
from app.modules.graph.review_projection_replay import replay_review_hypothesis_projections

_NOW = datetime(2026, 9, 22, tzinfo=UTC)


class _FakeOutbox:
    def __init__(self, events: list[ReviewProjectionEventRecord]) -> None:
        self._events = events
        self.succeeded: list = []
        self.retried: list = []

    async def claim_batch(self, *, now, lease_seconds, batch_size):
        return self._events

    async def mark_succeeded(self, event_id, now):
        self.succeeded.append(event_id)

    async def mark_retryable_failure(self, event_id, *, now, error_code, error_message):
        self.retried.append(event_id)


class _FakeGraph:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls = 0
        self._raise_error = raise_error

    async def write(self, query, parameters):
        self.calls += 1
        if self._raise_error:
            raise GraphConnectionError("graph write failed")
        return [{"ok": True}]


class _FakeReviewRepository:
    def __init__(self, decision=None) -> None:
        self._decision = decision

    async def get_decision(self, case_id, candidate_link_id):
        return self._decision


class _FakeIntegrationRepository:
    def __init__(self, *, candidate=None, event=None) -> None:
        self._candidate = candidate
        self._event = event

    async def get_candidate_link(self, case_id, candidate_link_id):
        return self._candidate

    async def get_event_for_correlation(self, case_id, correlation_id):
        return self._event


class _Event:
    projection_key = "synthetic-projection-key"


class _FakeHypothesisRepository:
    def __init__(self, hypothesis=None) -> None:
        self._hypothesis = hypothesis

    async def get_hypothesis(self, case_id, hypothesis_id):
        return self._hypothesis


def _outbox_event(case_id, subject_type, subject_id) -> ReviewProjectionEventRecord:
    return ReviewProjectionEventRecord(
        event_id=uuid4(),
        case_id=case_id,
        subject_type=subject_type,
        subject_id=subject_id,
        status=_Status.RUNNING,
        attempt=1,
        max_attempts=5,
        lease_expires_at=None,
        last_error_code=None,
        last_error_message=None,
        created_at=_NOW,
        updated_at=_NOW,
        completed_at=None,
    )


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


async def test_candidate_review_decision_replays_successfully() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    event = _outbox_event(
        case_id, ReviewProjectionSubjectType.CANDIDATE_REVIEW_DECISION, candidate.candidate_link_id
    )
    outbox = _FakeOutbox([event])
    graph = _FakeGraph()

    summary = await replay_review_hypothesis_projections(
        outbox,
        review_repository=_FakeReviewRepository(decision=decision),
        hypothesis_repository=_FakeHypothesisRepository(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=_Event()),
        graph_repository=graph,
        postgres_engine=object(),
        now=_NOW,
    )

    assert summary.claimed == 1
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert graph.calls == 1
    assert outbox.succeeded == [event.event_id]


async def test_candidate_review_decision_defers_when_correlation_not_projected_yet() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    event = _outbox_event(
        case_id, ReviewProjectionSubjectType.CANDIDATE_REVIEW_DECISION, candidate.candidate_link_id
    )
    outbox = _FakeOutbox([event])

    summary = await replay_review_hypothesis_projections(
        outbox,
        review_repository=_FakeReviewRepository(decision=decision),
        hypothesis_repository=_FakeHypothesisRepository(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=None),
        graph_repository=_FakeGraph(),
        postgres_engine=object(),
        now=_NOW,
    )

    assert summary.succeeded == 0
    assert summary.failed == 1
    assert outbox.retried == [event.event_id]


async def test_candidate_review_decision_vacuously_succeeds_if_decision_no_longer_exists() -> None:
    case_id = uuid4()
    event = _outbox_event(case_id, ReviewProjectionSubjectType.CANDIDATE_REVIEW_DECISION, uuid4())
    outbox = _FakeOutbox([event])

    summary = await replay_review_hypothesis_projections(
        outbox,
        review_repository=_FakeReviewRepository(decision=None),
        hypothesis_repository=_FakeHypothesisRepository(),
        integration_repository=_FakeIntegrationRepository(),
        graph_repository=_FakeGraph(),
        postgres_engine=object(),
        now=_NOW,
    )

    assert summary.succeeded == 1


async def test_graph_outage_marks_retryable_failure_not_a_crash() -> None:
    case_id = uuid4()
    candidate = _candidate(case_id)
    decision = _decision(case_id, candidate.candidate_link_id)
    event = _outbox_event(
        case_id, ReviewProjectionSubjectType.CANDIDATE_REVIEW_DECISION, candidate.candidate_link_id
    )
    outbox = _FakeOutbox([event])

    summary = await replay_review_hypothesis_projections(
        outbox,
        review_repository=_FakeReviewRepository(decision=decision),
        hypothesis_repository=_FakeHypothesisRepository(),
        integration_repository=_FakeIntegrationRepository(candidate=candidate, event=_Event()),
        graph_repository=_FakeGraph(raise_error=True),
        postgres_engine=object(),
        now=_NOW,
    )

    assert summary.succeeded == 0
    assert summary.failed == 1
    assert outbox.retried == [event.event_id]


async def test_hypothesis_review_decision_vacuous_when_hypothesis_missing() -> None:
    case_id = uuid4()
    event = _outbox_event(case_id, ReviewProjectionSubjectType.HYPOTHESIS_REVIEW_DECISION, uuid4())
    outbox = _FakeOutbox([event])

    summary = await replay_review_hypothesis_projections(
        outbox,
        review_repository=_FakeReviewRepository(),
        hypothesis_repository=_FakeHypothesisRepository(hypothesis=None),
        integration_repository=_FakeIntegrationRepository(),
        graph_repository=_FakeGraph(),
        postgres_engine=object(),
        now=_NOW,
    )

    assert summary.succeeded == 1


async def test_hypothesis_review_decision_replays_when_decided() -> None:
    case_id = uuid4()
    hypothesis_id = uuid4()
    hypothesis = HypothesisRecord(
        hypothesis_id=hypothesis_id,
        case_id=case_id,
        status=HypothesisStatus.ACCEPTED_BY_REVIEWER,
        created_by=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        decided_at=_NOW,
        decided_by=uuid4(),
        supporting_observation_ids=(uuid4(),),
        supporting_candidate_ids=(),
        statement="synthetic statement",
        statement_commitment_sha256="a" * 64,
        rationale=None,
        rationale_commitment_sha256=None,
    )
    event = _outbox_event(
        case_id, ReviewProjectionSubjectType.HYPOTHESIS_REVIEW_DECISION, hypothesis_id
    )
    outbox = _FakeOutbox([event])
    graph = _FakeGraph()

    summary = await replay_review_hypothesis_projections(
        outbox,
        review_repository=_FakeReviewRepository(),
        hypothesis_repository=_FakeHypothesisRepository(hypothesis=hypothesis),
        integration_repository=_FakeIntegrationRepository(),
        graph_repository=graph,
        postgres_engine=object(),
        now=_NOW,
    )

    assert summary.succeeded == 1
    assert graph.calls == 1
