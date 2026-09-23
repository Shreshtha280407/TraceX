"""Gap-Closure WP-4 (G3): handoff summary -- computed at read time, no new table."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.access_control.notes_models import CaseNoteRecord
from app.modules.graph.handoff_service import build_handoff_summary
from app.modules.graph.hypothesis_models import HypothesisRecord, HypothesisStatus
from app.modules.graph.integration_models import CandidateLinkRecord, PropositionStatus
from app.modules.graph.review_models import CandidateReviewDecisionRecord, CandidateReviewOutcome

_NOW = datetime(2026, 9, 22, tzinfo=UTC)


def _candidate(case_id) -> CandidateLinkRecord:
    return CandidateLinkRecord(
        candidate_link_id=uuid4(),
        correlation_id=uuid4(),
        case_id=case_id,
        idempotency_key=f"synthetic.{uuid4()}",
        left_observation_id=uuid4(),
        right_observation_id=uuid4(),
        status=PropositionStatus.NEEDS_REVIEW,
        reason_reference=None,
        evidence_paths=(),
        created_at=_NOW,
    )


def _decision(case_id, candidate_link_id, outcome) -> CandidateReviewDecisionRecord:
    return CandidateReviewDecisionRecord(
        candidate_review_decision_id=uuid4(),
        case_id=case_id,
        candidate_link_id=candidate_link_id,
        correlation_id=uuid4(),
        decision=outcome,
        reviewer_user_id=uuid4(),
        rationale=None,
        rationale_commitment_sha256=None,
        created_at=_NOW,
    )


def _hypothesis(case_id, status) -> HypothesisRecord:
    return HypothesisRecord(
        hypothesis_id=uuid4(),
        case_id=case_id,
        status=status,
        created_by=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        decided_at=None,
        decided_by=None,
        supporting_observation_ids=(uuid4(),),
        supporting_candidate_ids=(),
        supporting_entity_resolution_candidate_ids=(),
        statement="synthetic",
        statement_commitment_sha256="a" * 64,
        rationale=None,
        rationale_commitment_sha256=None,
    )


class _FakeIntegrationRepository:
    def __init__(self, candidates) -> None:
        self._candidates = candidates

    async def list_candidates(self, case_id, *, limit=None):
        return self._candidates


class _FakeReviewRepository:
    def __init__(self, decisions) -> None:
        self._decisions = decisions

    async def list_decisions(self, case_id, *, candidate_link_ids=None, limit=None):
        return self._decisions


class _FakeHypothesisRepository:
    def __init__(self, hypotheses) -> None:
        self._hypotheses = hypotheses

    async def list_hypotheses(self, case_id, *, limit=None):
        return self._hypotheses


class _FakeNoteRepository:
    def __init__(self, notes) -> None:
        self._notes = notes

    async def list_notes(self, case_id, *, limit=None):
        return self._notes


async def test_open_candidate_has_no_decision_and_is_counted_open() -> None:
    case_id = uuid4()
    open_candidate = _candidate(case_id)
    accepted_candidate = _candidate(case_id)
    accepted_decision = _decision(
        case_id, accepted_candidate.candidate_link_id, CandidateReviewOutcome.ACCEPTED_BY_REVIEWER
    )

    summary = await build_handoff_summary(
        case_id,
        integration_repository=_FakeIntegrationRepository([open_candidate, accepted_candidate]),
        review_repository=_FakeReviewRepository([accepted_decision]),
        hypothesis_repository=_FakeHypothesisRepository([]),
        note_repository=_FakeNoteRepository([]),
    )

    assert summary.open_candidate_count == 1
    assert summary.open_candidates[0].candidate_link_id == open_candidate.candidate_link_id
    assert summary.accepted_candidate_count == 1
    assert summary.rejected_candidate_count == 0


async def test_rejected_hypothesis_counted_but_not_listed_as_open() -> None:
    case_id = uuid4()
    open_h = _hypothesis(case_id, HypothesisStatus.NEEDS_REVIEW)
    rejected_h = _hypothesis(case_id, HypothesisStatus.REJECTED_BY_REVIEWER)

    summary = await build_handoff_summary(
        case_id,
        integration_repository=_FakeIntegrationRepository([]),
        review_repository=_FakeReviewRepository([]),
        hypothesis_repository=_FakeHypothesisRepository([open_h, rejected_h]),
        note_repository=_FakeNoteRepository([]),
    )

    assert len(summary.open_hypotheses) == 1
    assert summary.open_hypotheses[0].hypothesis_id == open_h.hypothesis_id
    assert summary.rejected_hypothesis_count == 1


async def test_notes_pass_through() -> None:
    case_id = uuid4()
    note = CaseNoteRecord(
        note_id=uuid4(),
        case_id=case_id,
        author_user_id=uuid4(),
        text="handoff note",
        supersedes_note_id=None,
        created_at=_NOW,
    )

    summary = await build_handoff_summary(
        case_id,
        integration_repository=_FakeIntegrationRepository([]),
        review_repository=_FakeReviewRepository([]),
        hypothesis_repository=_FakeHypothesisRepository([]),
        note_repository=_FakeNoteRepository([note]),
    )

    assert summary.recent_notes == (note,)
