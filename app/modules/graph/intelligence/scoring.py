"""Transparent preliminary rules scoring; all outputs remain review candidates."""

from __future__ import annotations

from dataclasses import dataclass

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
CONTRADICTION_WEIGHT = -40.0


@dataclass(frozen=True)
class RulesProfile:
    """One explicit, named, versioned rules configuration.

    A profile bundles exactly what makes two scoring runs comparable in
    the Phase 5 benchmark (`docs/decisions/ADR-006-phase-5-rules-
    baseline.md`): a version label and the named weight for each
    `RetrievalReason`/contradiction. Comparing profiles is how this
    project evaluates a candidate rules change -- never a trained model,
    never automatic reweighting.
    """

    version: str
    weights: dict[str, float]
    contradiction_weight: float = CONTRADICTION_WEIGHT

    def config_hash(self) -> str:
        return canonical_sha256(
            {
                "version": self.version,
                "weights": self.weights,
                "contradiction_weight": self.contradiction_weight,
            }
        )


BASELINE_RULES_PROFILE = RulesProfile(version=RULES_CONFIG_VERSION, weights=RULE_WEIGHTS)

# Preserved exactly for any existing caller reading these module constants
# directly (none exist outside this module today, but the names predate
# `RulesProfile` and are cheap to keep stable).
PRELIMINARY_RULES = {"version": RULES_CONFIG_VERSION, "weights": RULE_WEIGHTS}
RULES_CONFIG_HASH = BASELINE_RULES_PROFILE.config_hash()


def score_candidates(
    candidates: tuple[RetrievedCandidate, ...], *, profile: RulesProfile = BASELINE_RULES_PROFILE
) -> tuple[ScoredCandidate, ...]:
    """Score `candidates` under one named `RulesProfile` (the frozen baseline by default)."""
    config_hash = profile.config_hash()
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        if len(candidate.supporting_observation_ids) < 2:
            raise ValueError("candidate must retain two supporting observation references")
        contributions: list[FeatureContribution] = []
        for reason in candidate.reasons:
            weight = profile.weights.get(reason.value, 0.0)
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
                    weight=profile.contradiction_weight,
                    contribution=profile.contradiction_weight,
                    reason=contradiction,
                    observation_ids=candidate.supporting_observation_ids,
                )
            )
        total = sum(item.contribution for item in contributions)
        snapshot = {
            "candidate": candidate.model_dump(mode="json"),
            "contributions": [item.model_dump(mode="json") for item in contributions],
            "rules_config_hash": config_hash,
        }
        snapshot_hash = canonical_sha256(snapshot)
        candidate_key = str(
            deterministic_uuid(
                "phase5_scored_candidate",
                str(candidate.case_id),
                str(candidate.left_observation_id),
                str(candidate.right_observation_id),
                config_hash,
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
                rules_config_version=profile.version,
                rules_config_hash=config_hash,
                explanation=(
                    "Review-only candidate; score is evidence-link guidance, "
                    "never identity verification."
                ),
            )
        )
    return tuple(scored)
