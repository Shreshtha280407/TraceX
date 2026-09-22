"""Gap-Closure WP-4 (G13): claim-and-retry loop for the review/hypothesis
projection outbox. Mirrors `integration_projector.replay_graph_updates`'
shape (claim a bounded batch, attempt each, mark succeeded/retryable-failure),
applied to `review_projection_outbox.ReviewProjectionOutboxRepository`
instead of the correlation outbox -- a deliberately separate, smaller
replay path (see the migration's own docstring for why not shared).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.hypothesis_repository import HypothesisRepository
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.review_projection import (
    project_candidate_review_decision,
    project_hypothesis,
    project_hypothesis_candidate_reference,
    project_hypothesis_review_decision,
)
from app.modules.graph.review_projection_outbox import (
    ReviewProjectionEventRecord,
    ReviewProjectionOutboxRepository,
    ReviewProjectionSubjectType,
)
from app.modules.graph.review_repository import CandidateReviewRepository
from app.modules.graph.review_service import resolve_evidence_paths

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReviewProjectionReplaySummary:
    claimed: int
    succeeded: int
    failed: int


async def _replay_candidate_review_decision(
    event: ReviewProjectionEventRecord,
    *,
    review_repository: CandidateReviewRepository,
    integration_repository: GraphCorrelationIntegrationRepository,
    graph_repository: Neo4jGraphRepository,
) -> bool:
    # `event.subject_id` is the `candidate_link_id` (see `review_service.
    # submit_candidate_review_decision`'s enqueue call): `get_decision`
    # already exists keyed that way, and a candidate has at most one
    # decision -- no need for a second, decision-id-keyed lookup method.
    decision = await review_repository.get_decision(event.case_id, event.subject_id)
    if decision is None:
        return True  # nothing to project anymore -- vacuously done
    candidate = await integration_repository.get_candidate_link(
        event.case_id, decision.candidate_link_id
    )
    if candidate is None:
        return True
    proj_event = await integration_repository.get_event_for_correlation(
        event.case_id, candidate.correlation_id
    )
    if proj_event is None:
        return False  # correlation not projected yet -- retry later
    await project_candidate_review_decision(
        graph_repository,
        case_id=event.case_id,
        projection_key=proj_event.projection_key,
        decision=decision,
    )
    return True


async def _replay_hypothesis_created(
    event: ReviewProjectionEventRecord,
    *,
    hypothesis_repository: HypothesisRepository,
    integration_repository: GraphCorrelationIntegrationRepository,
    graph_repository: Neo4jGraphRepository,
    postgres_engine: AsyncEngine,
) -> bool:
    hypothesis = await hypothesis_repository.get_hypothesis(event.case_id, event.subject_id)
    if hypothesis is None:
        return True
    evidence_paths = await resolve_evidence_paths(
        postgres_engine,
        case_id=hypothesis.case_id,
        observation_ids=hypothesis.supporting_observation_ids,
    )
    projected = await project_hypothesis(
        graph_repository, hypothesis=hypothesis, evidence_paths=evidence_paths
    )
    if not projected:
        return False
    for candidate_id in hypothesis.supporting_candidate_ids:
        candidate = await integration_repository.get_candidate_link(
            hypothesis.case_id, candidate_id
        )
        if candidate is None:
            continue
        proj_event = await integration_repository.get_event_for_correlation(
            hypothesis.case_id, candidate.correlation_id
        )
        if proj_event is not None:
            await project_hypothesis_candidate_reference(
                graph_repository,
                case_id=hypothesis.case_id,
                hypothesis_id=hypothesis.hypothesis_id,
                projection_key=proj_event.projection_key,
            )
    return True


async def _replay_hypothesis_review_decision(
    event: ReviewProjectionEventRecord,
    *,
    hypothesis_repository: HypothesisRepository,
    graph_repository: Neo4jGraphRepository,
) -> bool:
    hypothesis = await hypothesis_repository.get_hypothesis(event.case_id, event.subject_id)
    if hypothesis is None or hypothesis.decided_at is None or hypothesis.decided_by is None:
        return True
    await project_hypothesis_review_decision(
        graph_repository,
        case_id=event.case_id,
        hypothesis_id=event.subject_id,
        status=hypothesis.status.value,
        decided_at=hypothesis.decided_at,
        decided_by=hypothesis.decided_by,
        rationale_commitment_sha256=hypothesis.rationale_commitment_sha256,
    )
    return True


async def replay_review_hypothesis_projections(
    outbox: ReviewProjectionOutboxRepository,
    *,
    review_repository: CandidateReviewRepository,
    hypothesis_repository: HypothesisRepository,
    integration_repository: GraphCorrelationIntegrationRepository,
    graph_repository: Neo4jGraphRepository,
    postgres_engine: AsyncEngine,
    lease_seconds: int = 120,
    batch_size: int = 25,
    now: datetime | None = None,
) -> ReviewProjectionReplaySummary:
    now = now or datetime.now(UTC)
    events = await outbox.claim_batch(now=now, lease_seconds=lease_seconds, batch_size=batch_size)
    succeeded = 0
    failed = 0
    for event in events:
        try:
            if event.subject_type is ReviewProjectionSubjectType.CANDIDATE_REVIEW_DECISION:
                ok = await _replay_candidate_review_decision(
                    event,
                    review_repository=review_repository,
                    integration_repository=integration_repository,
                    graph_repository=graph_repository,
                )
            elif event.subject_type is ReviewProjectionSubjectType.HYPOTHESIS_CREATED:
                ok = await _replay_hypothesis_created(
                    event,
                    hypothesis_repository=hypothesis_repository,
                    integration_repository=integration_repository,
                    graph_repository=graph_repository,
                    postgres_engine=postgres_engine,
                )
            else:
                ok = await _replay_hypothesis_review_decision(
                    event,
                    hypothesis_repository=hypothesis_repository,
                    graph_repository=graph_repository,
                )
            if ok:
                await outbox.mark_succeeded(event.event_id, now)
                succeeded += 1
            else:
                await outbox.mark_retryable_failure(
                    event.event_id,
                    now=now,
                    error_code="dependency_not_projected_yet",
                    error_message="a required upstream projection has not completed yet",
                )
                failed += 1
        except GraphConnectionError as exc:
            logger.warning(
                "graph.review_hypothesis_replay_failed",
                event_id=str(event.event_id),
                subject_type=event.subject_type.value,
                exc_type=type(exc).__name__,
            )
            await outbox.mark_retryable_failure(
                event.event_id,
                now=now,
                error_code="graph_connection_error",
                error_message=type(exc).__name__,
            )
            failed += 1
    return ReviewProjectionReplaySummary(claimed=len(events), succeeded=succeeded, failed=failed)


__all__ = ["ReviewProjectionReplaySummary", "replay_review_hypothesis_projections"]
