"""Transparent preliminary rules scoring; all outputs remain review candidates."""

from __future__ import annotations

from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.graph.intelligence.models import (
    CandidateStatus,
    FeatureContribution,
    RetrievedCandidate,
    ScoredCandidate,
)

RULES_CONFIG_VERSION = "phase5_preliminary_rules_v1"
RULE_WEIGHTS: dict[str, float] = {
    "exact_identifier": 60.0,
    "platform_scoped_handle": 45.0,
    "exact_alias": 12.0,
    "normalized_alias": 8.0,
    "transliteration_candidate": 6.0,
    "local_vector_candidate": 3.0,
    "temporal_hot_window": 10.0,
    "contradiction": -40.0,
}
PRELIMINARY_RULES = {"version": RULES_CONFIG_VERSION, "weights": RULE_WEIGHTS}
RULES_CONFIG_HASH = canonical_sha256(PRELIMINARY_RULES)


def score_candidates(candidates: tuple[RetrievedCandidate, ...]) -> tuple[ScoredCandidate, ...]:
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        if len(candidate.supporting_observation_ids) < 2:
            raise ValueError("candidate must retain two supporting observation references")
        contributions: list[FeatureContribution] = []
        for reason in candidate.reasons:
            weight = RULE_WEIGHTS.get(reason.value, 0.0)
            contributions.append(
                FeatureContribution(
                    feature=reason.value,
                    weight=weight,
                    contribution=weight,
                    reason=f"retrieved by {reason.value}",
                    observation_ids=candidate.supporting_observation_ids,
                )
            )
        for contradiction in candidate.contradiction_reasons:
            contributions.append(
                FeatureContribution(
                    feature="contradiction",
                    weight=-40.0,
                    contribution=-40.0,
                    reason=contradiction,
                    observation_ids=candidate.supporting_observation_ids,
                )
            )
        total = sum(item.contribution for item in contributions)
        snapshot = {
            "candidate": candidate.model_dump(mode="json"),
            "contributions": [item.model_dump(mode="json") for item in contributions],
            "rules_config_hash": RULES_CONFIG_HASH,
        }
        snapshot_hash = canonical_sha256(snapshot)
        candidate_key = str(
            deterministic_uuid(
                "phase5_scored_candidate",
                str(candidate.case_id),
                str(candidate.left_observation_id),
                str(candidate.right_observation_id),
                RULES_CONFIG_HASH,
                snapshot_hash,
            )
        )
        status = (
            CandidateStatus.NEEDS_REVIEW
            if candidate.contradiction_reasons
            else CandidateStatus.CANDIDATE
        )
        scored.append(
            ScoredCandidate(
                case_id=candidate.case_id,
                candidate_key=candidate_key,
                left_observation_id=candidate.left_observation_id,
                right_observation_id=candidate.right_observation_id,
                status=status,
                total_rules_score=total,
                contributions=tuple(contributions),
                contradiction_reasons=candidate.contradiction_reasons,
                supporting_observation_ids=candidate.supporting_observation_ids,
                supporting_evidence_ids=candidate.supporting_evidence_ids,
                feature_snapshot_hash=snapshot_hash,
                rules_config_version=RULES_CONFIG_VERSION,
                rules_config_hash=RULES_CONFIG_HASH,
                explanation=(
                    "Review-only candidate; score is evidence-link guidance, "
                    "never identity verification."
                ),
            )
        )
    return tuple(scored)
