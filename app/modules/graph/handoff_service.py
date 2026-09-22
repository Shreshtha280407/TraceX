"""Gap-Closure WP-4 (G3): case handoff summary -- computed at read time.

No new table (per the plan's own instruction): this aggregates already-
durable data -- candidate/hypothesis review decisions, still-open
candidates/hypotheses, and case notes -- exactly the "verified/rejected/
needs-more-evidence decisions, open candidates, notes" the plan names.
Bounded: every list here is capped, mirroring every other case-scoped read
in this codebase.
"""

from __future__ import annotations

from uuid import UUID

from app.modules.access_control.notes_models import CaseNoteRecord
from app.modules.access_control.notes_repository import CaseNoteRepository
from app.modules.graph.hypothesis_models import HypothesisRecord, HypothesisStatus
from app.modules.graph.hypothesis_repository import HypothesisRepository
from app.modules.graph.integration_models import CandidateLinkRecord
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.models import GraphModel
from app.modules.graph.review_models import CandidateReviewOutcome
from app.modules.graph.review_repository import CandidateReviewRepository

_HANDOFF_LIST_LIMIT = 50


class HandoffSummary(GraphModel):
    case_id: UUID
    open_candidate_count: int
    open_candidates: tuple[CandidateLinkRecord, ...]
    accepted_candidate_count: int
    rejected_candidate_count: int
    open_hypotheses: tuple[HypothesisRecord, ...]
    accepted_hypothesis_count: int
    rejected_hypothesis_count: int
    recent_notes: tuple[CaseNoteRecord, ...]


async def build_handoff_summary(
    case_id: UUID,
    *,
    integration_repository: GraphCorrelationIntegrationRepository,
    review_repository: CandidateReviewRepository,
    hypothesis_repository: HypothesisRepository,
    note_repository: CaseNoteRepository,
) -> HandoffSummary:
    candidates = await integration_repository.list_candidates(case_id, limit=_HANDOFF_LIST_LIMIT)
    candidate_ids = tuple(c.candidate_link_id for c in candidates)
    decisions = {
        d.candidate_link_id: d
        for d in await review_repository.list_decisions(
            case_id, candidate_link_ids=candidate_ids, limit=_HANDOFF_LIST_LIMIT
        )
    }
    open_candidates = tuple(c for c in candidates if decisions.get(c.candidate_link_id) is None)
    accepted_candidates = sum(
        1 for d in decisions.values() if d.decision is CandidateReviewOutcome.ACCEPTED_BY_REVIEWER
    )
    rejected_candidates = sum(
        1 for d in decisions.values() if d.decision is CandidateReviewOutcome.REJECTED_BY_REVIEWER
    )

    hypotheses = await hypothesis_repository.list_hypotheses(case_id, limit=_HANDOFF_LIST_LIMIT)
    open_hypotheses = tuple(h for h in hypotheses if h.status is HypothesisStatus.NEEDS_REVIEW)
    accepted_hypotheses = sum(
        1 for h in hypotheses if h.status is HypothesisStatus.ACCEPTED_BY_REVIEWER
    )
    rejected_hypotheses = sum(
        1 for h in hypotheses if h.status is HypothesisStatus.REJECTED_BY_REVIEWER
    )

    notes = await note_repository.list_notes(case_id, limit=_HANDOFF_LIST_LIMIT)

    return HandoffSummary(
        case_id=case_id,
        open_candidate_count=len(open_candidates),
        open_candidates=open_candidates,
        accepted_candidate_count=accepted_candidates,
        rejected_candidate_count=rejected_candidates,
        open_hypotheses=open_hypotheses,
        accepted_hypothesis_count=accepted_hypotheses,
        rejected_hypothesis_count=rejected_hypotheses,
        recent_notes=tuple(notes),
    )


__all__ = ["HandoffSummary", "build_handoff_summary"]
