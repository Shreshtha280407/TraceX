"""Case-scoped orchestration: real observations -> candidates -> Nipun's durable seam.

This is the second half of the missing wiring the Phase 5A reconciliation
audit found: `sourcing.py` adapts real data into the existing, already-real
`retrieval.py`/`scoring.py`/`correlation.py`/`analytics.py` primitives; this
module chains them into one callable pass per case and, for the correlation
path, submits the result through Nipun's existing
`GraphCorrelationIntegrationRepository.submit()` -- never a second
persistence route, never a direct Neo4j write.

Every function here takes an explicit `case_id` and only ever reads/writes
data scoped to it (`fetch_case_observations`/`list_candidates` are
themselves SQL-scoped by `case_id`) -- there is no code path in this module
that can see or mix two cases' data.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from app.contracts.observation import ObservationV1
from app.modules.graph.integration_models import (
    CorrelationSubmission,
    CorrelationSubmissionReceipt,
)
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.intelligence.analytics import (
    analyse,
    detect_communication_transfer_movement_motifs,
)
from app.modules.graph.intelligence.correlation import (
    add_temporal_hot_window_reason,
    build_correlation_submission,
)
from app.modules.graph.intelligence.models import AnalyticsResult, MotifMatch
from app.modules.graph.intelligence.retrieval import retrieve_candidates
from app.modules.graph.intelligence.scoring import score_candidates
from app.modules.graph.intelligence.sourcing import (
    build_motif_edges,
    descriptors_from_observation,
    edges_from_candidate_links,
    fetch_case_observations,
)
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission
from app.modules.integrity.service import IntegrityService

logger = structlog.get_logger(__name__)

#: Canonical-metadata shape version for this module's integrity producer
#: seam (Phase 6) -- built from IDs/counts/status only, never observation
#: text. See `docs/architecture/phase-6-integrity.md`.
CORRELATION_COMPLETED_SCHEMA_VERSION = "correlation_completed.v1"


async def _record_correlation_integrity_event_safely(
    integrity_recorder: IntegrityService | None,
    receipt: CorrelationSubmissionReceipt,
    *,
    case_id: UUID,
    now: datetime | None,
) -> None:
    """Best-effort integrity recording for a genuinely new correlation.

    A replayed submission is skipped -- it was already recorded (with the
    same idempotency key) the first time it was submitted. A failure here
    never affects the already-committed correlation this follows, mirroring
    `evidence_lifecycle.service._record_integrity_event_safely`.
    """
    if integrity_recorder is None or receipt.replayed:
        return
    correlation = receipt.correlation
    try:
        await integrity_recorder.record_integrity_event(
            IntegrityEventSubmission(
                case_id=case_id,
                event_kind=IntegrityEventKind.CORRELATION_COMPLETED,
                subject_type="correlation",
                subject_id=str(correlation.correlation_id),
                canonical_metadata={
                    "correlation_id": str(correlation.correlation_id),
                    "correlation_type": correlation.correlation_type,
                    "status": correlation.status.value,
                    "supporting_observation_ids": [
                        str(v) for v in correlation.supporting_observation_ids
                    ],
                    "contradictory_observation_ids": [
                        str(v) for v in correlation.contradictory_observation_ids
                    ],
                    "mapping_version": correlation.mapping_version,
                    "config_version": correlation.config_version,
                },
                payload_schema_version=CORRELATION_COMPLETED_SCHEMA_VERSION,
                source_created_at=correlation.created_at,
                idempotency_key=correlation.idempotency_key,
            )
        )
    except Exception as exc:
        logger.warning(
            "integrity.event_record_failed",
            case_id=str(case_id),
            event_kind=IntegrityEventKind.CORRELATION_COMPLETED.value,
            subject_type="correlation",
            subject_id=str(correlation.correlation_id),
            retry_state="reconciliation_pending",
            failure_category=type(exc).__name__,
        )


def build_case_correlation_submission(
    observations: list[ObservationV1], case_id: UUID
) -> CorrelationSubmission | None:
    """Pure: real observations for one case -> at most one `CorrelationSubmission`.

    Returns `None` (never an error) when fewer than two observations carry
    retrieval-eligible signal, or when none of them share a retrieval
    reason -- both are normal, expected outcomes for a case with too little
    correlatable evidence yet, not a failure.
    """
    if any(observation.case_id != case_id for observation in observations):
        raise ValueError("all observations passed to a case correlation pass must match case_id")
    descriptors = tuple(
        descriptor
        for observation in observations
        for descriptor in descriptors_from_observation(observation)
    )
    if len(descriptors) < 2:
        return None
    candidates = retrieve_candidates(list(descriptors))
    if not candidates:
        return None
    candidates = add_temporal_hot_window_reason(candidates, descriptors)
    scored = score_candidates(candidates)
    return build_correlation_submission(scored)


async def run_case_correlation_pass(
    repository: GraphCorrelationIntegrationRepository,
    engine: AsyncEngine,
    case_id: UUID,
    *,
    now: datetime | None = None,
    integrity_recorder: IntegrityService | None = None,
) -> CorrelationSubmissionReceipt | None:
    """I/O: fetch this case's canonical observations, build a submission, and
    submit it through Nipun's durable seam exactly once.

    Idempotent by construction: re-running this against the same case's
    unchanged observations rebuilds the identical `CorrelationSubmission`
    (same descriptors -> same candidates -> same scores -> same
    idempotency_key), so `repository.submit()`'s own replay path returns the
    existing receipt rather than creating a duplicate correlation.

    `integrity_recorder` is optional (Phase 6, absent from every pre-existing
    caller/test) and, when present, only records a `correlation_completed`
    integrity event for a genuinely new (non-replayed) submission.
    """
    observations = await fetch_case_observations(engine, case_id)
    submission = build_case_correlation_submission(observations, case_id)
    if submission is None:
        return None
    receipt = await repository.submit(case_id=case_id, submission=submission, now=now)
    await _record_correlation_integrity_event_safely(
        integrity_recorder, receipt, case_id=case_id, now=now
    )
    return receipt


async def run_case_analytics_snapshot(
    repository: GraphCorrelationIntegrationRepository, case_id: UUID, *, seed: int = 0
) -> tuple[AnalyticsResult, ...]:
    """PageRank/betweenness/WCC/Leiden over one case's already-persisted candidate-link graph.

    Never touches raw observations directly -- the analytics graph is the
    reviewable candidate-link graph Nipun's durable seam already persisted,
    never a hypothetical entity/identity graph (see `sourcing.
    edges_from_candidate_links`).
    """
    links = await repository.list_candidates(case_id)
    edges = edges_from_candidate_links(links)
    if not edges:
        return ()
    return analyse(edges, seed=seed)


async def run_case_motif_snapshot(
    engine: AsyncEngine, case_id: UUID, *, window_seconds: int = 3_600
) -> tuple[MotifMatch, ...]:
    """The bounded communication -> transfer -> movement/meeting motif for one case."""
    observations = await fetch_case_observations(engine, case_id)
    edges = build_motif_edges(observations)
    if not edges:
        return ()
    return detect_communication_transfer_movement_motifs(edges, window_seconds=window_seconds)


__all__ = [
    "build_case_correlation_submission",
    "run_case_analytics_snapshot",
    "run_case_correlation_pass",
    "run_case_motif_snapshot",
]
