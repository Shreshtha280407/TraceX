"""Phase 6 Part 5 orchestration: durable write -> integrity event -> Neo4j projection.

Each function performs exactly one already-committed primary write (via a
repository), then two best-effort, non-blocking side effects -- an
integrity event and a Neo4j projection -- mirroring
`graph.intelligence.pipeline._record_correlation_integrity_event_safely`'s
established contract: the primary write has already durably succeeded by
the time either side effect runs, so a failure in either is logged and
never re-raised, never rolls back the decision/hypothesis itself. Both
failure modes are reconciliation-shaped (see
`app/modules/integrity/reconciliation.py`'s new review/hypothesis scan
branches for the integrity side; there is no equivalent durable retry queue
for the Neo4j projection side yet -- see `docs/qa/known-limitations.md`).

`app/modules/graph/api.py` stays thin per its own docstring: it calls
exactly one function here per route and translates the small set of typed
domain exceptions into HTTP responses.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.evidence_lifecycle.repository import worker_observations_table
from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.hypothesis_models import (
    HypothesisCreateSubmission,
    HypothesisRecord,
    HypothesisReviewSubmission,
)
from app.modules.graph.hypothesis_repository import HypothesisRepository
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.review_models import (
    CandidateReviewDecisionSubmission,
    CandidateReviewView,
    candidate_review_view,
)
from app.modules.graph.review_projection import (
    project_candidate_review_decision,
    project_hypothesis,
    project_hypothesis_candidate_reference,
    project_hypothesis_review_decision,
)
from app.modules.graph.review_projection_outbox import (
    ReviewProjectionOutboxRepository,
    ReviewProjectionSubjectType,
)
from app.modules.graph.review_repository import CandidateReviewRepository
from app.modules.integrity.models import IntegrityEventSubmission
from app.modules.integrity.service import IntegrityService

logger = structlog.get_logger(__name__)


class CandidateNotFoundError(ValueError):
    """No candidate link exists for this case/ID -- the API layer renders this as 404."""


async def _record_integrity_event_safely(
    integrity_service: IntegrityService, submission: IntegrityEventSubmission
) -> None:
    try:
        await integrity_service.record_integrity_event(submission)
    except Exception as exc:  # noqa: BLE001 - a side-effect failure must never propagate
        logger.warning(
            "integrity.event_record_failed",
            case_id=str(submission.case_id),
            event_kind=submission.event_kind.value,
            subject_type=submission.subject_type,
            subject_id=submission.subject_id,
            retry_state="reconciliation_pending",
            failure_category=type(exc).__name__,
        )


async def _enqueue_for_durable_replay(
    outbox: ReviewProjectionOutboxRepository,
    *,
    case_id: UUID,
    subject_type: ReviewProjectionSubjectType,
    subject_id: UUID,
    now: datetime,
) -> UUID:
    """Always durable-first (G13): enqueue before attempting the immediate,
    best-effort projection. If the immediate attempt fails or the process
    crashes mid-attempt, this row stays `queued`/`running` and
    `intelligence_worker.py`'s review/hypothesis replay action retries it
    later -- a Neo4j outage no longer silently drops the projection."""
    event = await outbox.enqueue(
        case_id=case_id, subject_type=subject_type, subject_id=subject_id, now=now
    )
    return event.event_id


async def resolve_evidence_paths(
    engine: AsyncEngine, *, case_id: UUID, observation_ids: tuple[UUID, ...]
) -> tuple[tuple[UUID, UUID], ...]:
    """`(evidence_id, observation_id)` for every requested ID, re-read from the
    canonical, case-scoped table -- never trusted from a caller."""
    if not observation_ids:
        return ()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(
                    worker_observations_table.c.evidence_id,
                    worker_observations_table.c.observation_id,
                ).where(
                    worker_observations_table.c.case_id == case_id,
                    worker_observations_table.c.observation_id.in_(observation_ids),
                )
            )
        ).all()
    return tuple((row.evidence_id, row.observation_id) for row in rows)


async def submit_candidate_review_decision(
    *,
    case_id: UUID,
    candidate_link_id: UUID,
    submission: CandidateReviewDecisionSubmission,
    reviewer_user_id: UUID,
    integration_repository: GraphCorrelationIntegrationRepository,
    review_repository: CandidateReviewRepository,
    graph_repository: Neo4jGraphRepository,
    integrity_service: IntegrityService,
    review_projection_outbox: ReviewProjectionOutboxRepository,
    now: datetime | None = None,
) -> CandidateReviewView:
    """Record one candidate review decision.

    Raises `CandidateNotFoundError` for a missing/cross-case candidate (the
    API layer renders 404) and lets `review_models.ReviewConflictError`
    propagate unchanged (the API layer renders 409) -- authorization has
    already run in the route dependency before this is ever called.
    """
    candidate = await integration_repository.get_candidate_link(case_id, candidate_link_id)
    if candidate is None:
        raise CandidateNotFoundError("candidate not found")

    decision, is_new = await review_repository.record_decision(
        case_id=case_id,
        candidate_link_id=candidate_link_id,
        correlation_id=candidate.correlation_id,
        decision=submission.decision,
        reviewer_user_id=reviewer_user_id,
        rationale=submission.rationale,
        now=now,
    )
    if is_new:
        await _record_integrity_event_safely(integrity_service, decision.to_integrity_submission())
        event = await integration_repository.get_event_for_correlation(
            case_id, candidate.correlation_id
        )
        if event is not None:
            outbox_event_id = await _enqueue_for_durable_replay(
                review_projection_outbox,
                case_id=case_id,
                subject_type=ReviewProjectionSubjectType.CANDIDATE_REVIEW_DECISION,
                subject_id=candidate_link_id,
                now=decision.created_at,
            )
            try:
                await project_candidate_review_decision(
                    graph_repository,
                    case_id=case_id,
                    projection_key=event.projection_key,
                    decision=decision,
                )
                await review_projection_outbox.mark_succeeded(outbox_event_id, decision.created_at)
            except GraphConnectionError as exc:
                logger.warning(
                    "graph.review_projection_failed",
                    case_id=str(case_id),
                    candidate_link_id=str(candidate_link_id),
                    exc_type=type(exc).__name__,
                )
                await review_projection_outbox.mark_retryable_failure(
                    outbox_event_id,
                    now=decision.created_at,
                    error_code="graph_connection_error",
                    error_message=type(exc).__name__,
                )
    return candidate_review_view(candidate, decision)


async def _best_effort_project_hypothesis(
    hypothesis: HypothesisRecord,
    *,
    integration_repository: GraphCorrelationIntegrationRepository,
    postgres_engine: AsyncEngine,
    graph_repository: Neo4jGraphRepository,
    review_projection_outbox: ReviewProjectionOutboxRepository,
) -> None:
    outbox_event_id = await _enqueue_for_durable_replay(
        review_projection_outbox,
        case_id=hypothesis.case_id,
        subject_type=ReviewProjectionSubjectType.HYPOTHESIS_CREATED,
        subject_id=hypothesis.hypothesis_id,
        now=hypothesis.created_at,
    )
    try:
        evidence_paths = await resolve_evidence_paths(
            postgres_engine,
            case_id=hypothesis.case_id,
            observation_ids=hypothesis.supporting_observation_ids,
        )
        projected = await project_hypothesis(
            graph_repository, hypothesis=hypothesis, evidence_paths=evidence_paths
        )
        if not projected:
            return
        # ADR-031: `supporting_entity_resolution_candidate_ids` is
        # deliberately never projected here -- WP-2 entity-resolution
        # candidates have no Neo4j node kind at all (entity_service.py
        # never touches `graph_repository`, by design, to stay decoupled
        # from Phase 5's Gate-C-bound scoring/correlation pipeline). This
        # loop stays scoped to `supporting_candidate_ids` (Phase 5
        # correlation candidates, which do have a projected `Correlation`
        # node) exactly as before.
        for candidate_id in hypothesis.supporting_candidate_ids:
            candidate = await integration_repository.get_candidate_link(
                hypothesis.case_id, candidate_id
            )
            if candidate is None:
                continue
            event = await integration_repository.get_event_for_correlation(
                hypothesis.case_id, candidate.correlation_id
            )
            if event is not None:
                await project_hypothesis_candidate_reference(
                    graph_repository,
                    case_id=hypothesis.case_id,
                    hypothesis_id=hypothesis.hypothesis_id,
                    projection_key=event.projection_key,
                )
        await review_projection_outbox.mark_succeeded(outbox_event_id, hypothesis.created_at)
    except GraphConnectionError as exc:
        logger.warning(
            "graph.hypothesis_projection_failed",
            case_id=str(hypothesis.case_id),
            hypothesis_id=str(hypothesis.hypothesis_id),
            exc_type=type(exc).__name__,
        )
        await review_projection_outbox.mark_retryable_failure(
            outbox_event_id,
            now=hypothesis.created_at,
            error_code="graph_connection_error",
            error_message=type(exc).__name__,
        )


async def create_hypothesis(
    *,
    case_id: UUID,
    submission: HypothesisCreateSubmission,
    created_by: UUID,
    hypothesis_repository: HypothesisRepository,
    integration_repository: GraphCorrelationIntegrationRepository,
    postgres_engine: AsyncEngine,
    graph_repository: Neo4jGraphRepository,
    integrity_service: IntegrityService,
    review_projection_outbox: ReviewProjectionOutboxRepository,
    now: datetime | None = None,
) -> HypothesisRecord:
    """Create one hypothesis. Lets `HypothesisValidationError` propagate
    unchanged (the API layer renders 422) for a missing/cross-case reference."""
    hypothesis, action, is_new = await hypothesis_repository.create_hypothesis(
        case_id=case_id,
        statement=submission.statement,
        rationale=submission.rationale,
        created_by=created_by,
        supporting_observation_ids=submission.supporting_observation_ids,
        supporting_candidate_ids=submission.supporting_candidate_ids,
        supporting_entity_resolution_candidate_ids=(
            submission.supporting_entity_resolution_candidate_ids
        ),
        now=now,
    )
    if is_new:
        await _record_integrity_event_safely(
            integrity_service, action.to_integrity_submission(hypothesis=hypothesis)
        )
        await _best_effort_project_hypothesis(
            hypothesis,
            integration_repository=integration_repository,
            postgres_engine=postgres_engine,
            graph_repository=graph_repository,
            review_projection_outbox=review_projection_outbox,
        )
    return hypothesis


async def submit_hypothesis_review_decision(
    *,
    case_id: UUID,
    hypothesis_id: UUID,
    submission: HypothesisReviewSubmission,
    reviewer_user_id: UUID,
    hypothesis_repository: HypothesisRepository,
    graph_repository: Neo4jGraphRepository,
    integrity_service: IntegrityService,
    review_projection_outbox: ReviewProjectionOutboxRepository,
    now: datetime | None = None,
) -> HypothesisRecord | None:
    """Record one hypothesis review decision. Returns `None` for a missing/
    cross-case hypothesis (the API layer renders 404); lets
    `hypothesis_models.HypothesisConflictError` propagate unchanged (409)."""
    result = await hypothesis_repository.record_review_decision(
        case_id=case_id,
        hypothesis_id=hypothesis_id,
        decision=submission.decision,
        reviewer_user_id=reviewer_user_id,
        rationale=submission.rationale,
        now=now,
    )
    if result is None:
        return None
    hypothesis, action, is_new = result
    if is_new:
        await _record_integrity_event_safely(
            integrity_service, action.to_integrity_submission(hypothesis=hypothesis)
        )
        decided_at, decided_by = hypothesis.decided_at, hypothesis.decided_by
        if decided_at is None or decided_by is None:
            # Invariant: `record_review_decision` always sets both together
            # with `is_new=True`. A violation here is a repository defect,
            # not a caller error -- skip the projection safely rather than
            # crash the request over an already-committed decision.
            logger.warning(
                "graph.hypothesis_review_missing_decision_fields",
                case_id=str(case_id),
                hypothesis_id=str(hypothesis_id),
            )
            return hypothesis
        outbox_event_id = await _enqueue_for_durable_replay(
            review_projection_outbox,
            case_id=case_id,
            subject_type=ReviewProjectionSubjectType.HYPOTHESIS_REVIEW_DECISION,
            subject_id=hypothesis_id,
            now=decided_at,
        )
        try:
            await project_hypothesis_review_decision(
                graph_repository,
                case_id=case_id,
                hypothesis_id=hypothesis_id,
                status=hypothesis.status.value,
                decided_at=decided_at,
                decided_by=decided_by,
                rationale_commitment_sha256=action.rationale_commitment_sha256,
            )
            await review_projection_outbox.mark_succeeded(outbox_event_id, decided_at)
        except GraphConnectionError as exc:
            logger.warning(
                "graph.hypothesis_review_projection_failed",
                case_id=str(case_id),
                hypothesis_id=str(hypothesis_id),
                exc_type=type(exc).__name__,
            )
            await review_projection_outbox.mark_retryable_failure(
                outbox_event_id,
                now=decided_at,
                error_code="graph_connection_error",
                error_message=type(exc).__name__,
            )
    return hypothesis


__all__ = [
    "CandidateNotFoundError",
    "create_hypothesis",
    "resolve_evidence_paths",
    "submit_candidate_review_decision",
    "submit_hypothesis_review_decision",
]
