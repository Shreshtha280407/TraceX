"""Temporal hot-window correlation and conversion to Nipun's typed seam."""

from __future__ import annotations

from datetime import timedelta

from app.core.canonical import canonical_sha256
from app.modules.graph.integration_models import (
    CandidateLinkSubmission,
    CorrelationSubmission,
    FeatureSnapshotSubmission,
    PropositionStatus,
)
from app.modules.graph.intelligence.models import (
    ObservationDescriptor,
    RetrievalReason,
    RetrievedCandidate,
    ScoredCandidate,
)

HOT_WINDOW_VERSION = "phase5_hot_window_v1"
HOT_WINDOW_SECONDS = 900


def _overlaps(left: ObservationDescriptor, right: ObservationDescriptor) -> bool:
    if (
        left.event_start is None
        or left.event_end is None
        or right.event_start is None
        or right.event_end is None
    ):
        return False
    return left.event_start <= right.event_end + timedelta(
        seconds=HOT_WINDOW_SECONDS
    ) and right.event_start <= left.event_end + timedelta(seconds=HOT_WINDOW_SECONDS)


def add_temporal_hot_window_reason(
    candidates: tuple[RetrievedCandidate, ...],
    descriptors: tuple[ObservationDescriptor, ...],
) -> tuple[RetrievedCandidate, ...]:
    """Add a source-time reason only when both bounded windows overlap.

    Missing or overly broad time data is unknown, never a temporal match.
    """
    by_observation = {item.observation_id: item for item in descriptors}
    enriched: list[RetrievedCandidate] = []
    for candidate in candidates:
        left = by_observation.get(candidate.left_observation_id)
        right = by_observation.get(candidate.right_observation_id)
        if (
            left is None
            or right is None
            or left.case_id != candidate.case_id
            or right.case_id != candidate.case_id
        ):
            raise ValueError("candidate lacks same-case provenance descriptors")
        reasons = candidate.reasons
        if _overlaps(left, right):
            reasons = tuple(sorted((*reasons, RetrievalReason.TEMPORAL), key=str))
        enriched.append(candidate.model_copy(update={"reasons": reasons}))
    return tuple(enriched)


def build_correlation_submission(
    scored: tuple[ScoredCandidate, ...], *, correlation_type: str = "rules_candidate_correlation"
) -> CorrelationSubmission:
    """Build a reproducible review-only submission for Nipun's durable boundary."""
    if not scored:
        raise ValueError("at least one scored candidate is required")
    case_ids = {candidate.case_id for candidate in scored}
    if len(case_ids) != 1:
        raise ValueError("correlation candidates span multiple cases")
    observation_ids = tuple(
        sorted(
            {item for candidate in scored for item in candidate.supporting_observation_ids}, key=str
        )
    )
    config_hash = canonical_sha256(
        {
            "hot_window_version": HOT_WINDOW_VERSION,
            "rules_config_hash": scored[0].rules_config_hash,
            "candidate_snapshots": [candidate.feature_snapshot_hash for candidate in scored],
        }
    )
    values = {
        "kind": "rules_candidate_correlation",
        "candidate_count": len(scored),
        "rules_config_hash": scored[0].rules_config_hash,
        "feature_snapshot_hashes": [candidate.feature_snapshot_hash for candidate in scored],
        "candidate_scores": [candidate.total_rules_score for candidate in scored],
        "supporting_evidence_ids": sorted(
            {str(item) for candidate in scored for item in candidate.supporting_evidence_ids}
        ),
        "candidate_only": True,
    }
    return CorrelationSubmission(
        idempotency_key=f"phase5.rules.{config_hash[:32]}",
        correlation_type=correlation_type,
        status=PropositionStatus.NEEDS_REVIEW,
        supporting_observation_ids=observation_ids,
        candidate_links=tuple(
            CandidateLinkSubmission(
                idempotency_key=f"candidate.{candidate.candidate_key}",
                left_observation_id=candidate.left_observation_id,
                right_observation_id=candidate.right_observation_id,
                status=PropositionStatus.NEEDS_REVIEW
                if candidate.status.value == "needs_review"
                else PropositionStatus.CANDIDATE,
                reason_reference="phase5.rules.v1",
            )
            for candidate in scored
        ),
        feature_snapshot=FeatureSnapshotSubmission(
            snapshot_version="phase5_rules_features_v1", config_hash=config_hash, values=values
        ),
        mapping_version="phase5_correlation_projection_v1",
        config_version=HOT_WINDOW_VERSION,
    )
