"""Phase 5 rules-baseline measurement and freeze experiment.

Runs the reproducible candidate-scoring experiment
`docs/decisions/ADR-006-phase-5-rules-baseline.md` deferred until all
Phase 5 contributors merged. Only controlled, synthetic, non-sensitive
fixtures (`tests/fixtures/graph/phase5_rules_benchmark.py`) -- no private
police data, no Operation Nightfall data. The frozen score this baseline
produces is a transparent investigation-priority signal, never a
probability of guilt, identity truth, or criminal association.

Records, per requirement: Precision@K, Recall@K, false-positive/false-link
count, contradiction behavior, reason-code stability, and
determinism/replay behavior -- for the existing baseline profile and one
explicit alternative configuration variant, so the freeze decision below
is evidence-based rather than assumed. See this file's own
`test_baseline_profile_is_frozen_after_this_benchmark` for the recorded
decision and `docs/decisions/ADR-006-phase-5-rules-baseline.md`'s updated
addendum for the narrative writeup.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.modules.graph.intelligence.models import CandidateStatus, RetrievedCandidate
from app.modules.graph.intelligence.retrieval import retrieve_candidates
from app.modules.graph.intelligence.scoring import (
    BASELINE_RULES_PROFILE,
    RulesProfile,
    score_candidates,
)
from app.modules.graph.intelligence.sourcing import descriptors_from_observation
from tests.fixtures.graph.phase5_rules_benchmark import (
    BenchmarkCase,
    build_benchmark,
    build_contradiction_probe,
    build_cross_case_attempt,
)

#: An explicit alternative configuration variant -- halves every positive
#: weight and removes the local vector signal entirely, to see whether a
#: materially different, still-transparent weighting changes this
#: benchmark's outcome at all.
_ALTERNATIVE_PROFILE = RulesProfile(
    version="phase5_rules_benchmark_alternative_v1",
    weights={
        "exact_identifier": 30.0,
        "platform_scoped_handle": 22.5,
        "exact_alias": 6.0,
        "normalized_alias": 4.0,
        "transliteration_candidate": 3.0,
        "local_vector_candidate": 0.0,
        "temporal_hot_window": 5.0,
    },
    contradiction_weight=-20.0,
)


def _retrieved_pairs(candidates: tuple[RetrievedCandidate, ...]) -> frozenset[frozenset[UUID]]:
    return frozenset(
        {frozenset({c.left_observation_id, c.right_observation_id}) for c in candidates}
    )


def _precision_recall_at_k(
    benchmark: BenchmarkCase, candidates: tuple[RetrievedCandidate, ...], *, k: int
) -> tuple[float, float, int]:
    """Precision@K/Recall@K/false-positive count ranked by `total_rules_score`."""
    scored = score_candidates(candidates)
    ranked = sorted(scored, key=lambda item: item.total_rules_score, reverse=True)[:k]
    ranked_pairs = frozenset(
        {frozenset({item.left_observation_id, item.right_observation_id}) for item in ranked}
    )
    true_positives = ranked_pairs & benchmark.positive_pairs
    false_positives = ranked_pairs - benchmark.positive_pairs
    precision = len(true_positives) / len(ranked_pairs) if ranked_pairs else 1.0
    recall = (
        len(true_positives) / len(benchmark.positive_pairs) if benchmark.positive_pairs else 1.0
    )
    return precision, recall, len(false_positives)


def _run(benchmark: BenchmarkCase, *, profile: RulesProfile) -> tuple[RetrievedCandidate, ...]:
    descriptors = [
        descriptor
        for observation in benchmark.observations
        for descriptor in descriptors_from_observation(observation)
    ]
    candidates = retrieve_candidates(descriptors)
    # `profile` only affects scoring, not retrieval -- exercised in the
    # scoring-only assertions below via `score_candidates(..., profile=...)`.
    del profile
    return candidates


def test_baseline_profile_achieves_perfect_precision_and_recall_on_the_benchmark() -> None:
    benchmark = build_benchmark()
    candidates = _run(benchmark, profile=BASELINE_RULES_PROFILE)
    retrieved = _retrieved_pairs(candidates)

    # Every labeled positive pair was found, and nothing else was.
    assert retrieved == benchmark.positive_pairs

    precision, recall, false_positive_count = _precision_recall_at_k(
        benchmark, candidates, k=len(benchmark.positive_pairs)
    )
    assert precision == 1.0
    assert recall == 1.0
    assert false_positive_count == 0


def test_same_event_pairs_are_never_candidates_false_link_check() -> None:
    """False-link check: the two ends of one two-party record (CDR
    caller/callee, finance sender/receiver) must never appear as a
    candidate pair with each other."""
    benchmark = build_benchmark()
    cdr_first, _cdr_second, finance_first, *_ = benchmark.observations
    cdr_parties = descriptors_from_observation(cdr_first)
    finance_parties = descriptors_from_observation(finance_first)
    assert len(cdr_parties) == 2
    assert len(finance_parties) == 2

    candidates = _run(benchmark, profile=BASELINE_RULES_PROFILE)
    retrieved = _retrieved_pairs(candidates)
    same_event_pair = frozenset({cdr_first.observation_id, cdr_first.observation_id})
    assert same_event_pair not in retrieved
    finance_same_event_pair = frozenset(
        {finance_first.observation_id, finance_first.observation_id}
    )
    assert finance_same_event_pair not in retrieved


def test_contradiction_is_recorded_and_downgrades_the_candidate_to_needs_review() -> None:
    left, right, left_id, right_id = build_contradiction_probe()
    candidates = retrieve_candidates([left, right])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert {candidate.left_observation_id, candidate.right_observation_id} == {left_id, right_id}
    assert "conflicting_phone_claim" in candidate.contradiction_reasons

    scored = score_candidates(candidates)
    assert len(scored) == 1
    assert scored[0].status is CandidateStatus.NEEDS_REVIEW
    contradiction_contributions = [
        c for c in scored[0].contributions if c.feature == "contradiction"
    ]
    assert len(contradiction_contributions) == 1
    assert contradiction_contributions[0].contribution < 0
    # A real match reason still fires alongside the contradiction -- a
    # contradiction downgrades a candidate to needs-review, it does not
    # silently discard genuine matching signal.
    assert scored[0].total_rules_score != 0.0


def test_cross_case_observations_are_never_silently_compared() -> None:
    """Cross-case attempt: even a would-be exact identifier match across
    two different cases must never reach `retrieve_candidates` together --
    case isolation is enforced before any scoring happens."""
    first, second = build_cross_case_attempt()
    assert first.case_id != second.case_id
    descriptors = [
        *descriptors_from_observation(first),
        *descriptors_from_observation(second),
    ]
    with pytest.raises(ValueError, match="multiple cases"):
        retrieve_candidates(descriptors)


def test_reason_codes_and_scores_are_stable_and_deterministic_across_repeated_runs() -> None:
    """Determinism/replay: rebuilding descriptors and re-running retrieval +
    scoring against the identical observations yields byte-identical
    output every time -- required for `pipeline.py`'s idempotent replay."""
    benchmark = build_benchmark()
    retrieved_a = _run(benchmark, profile=BASELINE_RULES_PROFILE)
    retrieved_b = _run(benchmark, profile=BASELINE_RULES_PROFILE)
    assert retrieved_a == retrieved_b
    assert [c.reasons for c in retrieved_a] == [c.reasons for c in retrieved_b]

    run_a = score_candidates(retrieved_a)
    run_b = score_candidates(retrieved_b)
    assert run_a == run_b
    assert [c.candidate_key for c in run_a] == [c.candidate_key for c in run_b]


def test_alternative_profile_variant_produces_the_same_retrieval_and_ranking() -> None:
    """Configuration-variant experiment: retrieval itself is profile-
    independent (only scoring reads weights), so the alternative variant's
    retrieved pairs are identical; only the numeric scores and per-feature
    contributions differ. Confirms the variant changes nothing about
    *which* candidates surface, only how they are weighted -- the
    dimension `docs/decisions/ADR-006-phase-5-rules-baseline.md`'s freeze
    decision below is actually about."""
    benchmark = build_benchmark()
    candidates = _run(benchmark, profile=BASELINE_RULES_PROFILE)

    baseline_scored = score_candidates(candidates, profile=BASELINE_RULES_PROFILE)
    alternative_scored = score_candidates(candidates, profile=_ALTERNATIVE_PROFILE)

    baseline_pairs = {(c.left_observation_id, c.right_observation_id) for c in baseline_scored}
    alternative_pairs = {
        (c.left_observation_id, c.right_observation_id) for c in alternative_scored
    }
    assert baseline_pairs == alternative_pairs

    # Halving every weight halves every score proportionally on this
    # benchmark (no reason interacts non-linearly) -- the ranking order
    # is unchanged; the alternative variant is not a safety regression,
    # but it is also not a measurable improvement on this fixture set.
    baseline_ranked = [
        (c.left_observation_id, c.right_observation_id)
        for c in sorted(baseline_scored, key=lambda item: item.total_rules_score, reverse=True)
    ]
    alternative_ranked = [
        (c.left_observation_id, c.right_observation_id)
        for c in sorted(alternative_scored, key=lambda item: item.total_rules_score, reverse=True)
    ]
    assert baseline_ranked == alternative_ranked


def test_baseline_profile_is_frozen_after_this_benchmark() -> None:
    """The recorded freeze decision (see `docs/decisions/ADR-006-phase-5-
    rules-baseline.md`'s updated addendum for the full narrative):

    The existing `phase5_preliminary_rules_v1` baseline achieves perfect
    Precision@K/Recall@K (1.0/1.0, 0 false positives/false links) on this
    benchmark, correctly suppresses same-event (two-party) self-pairing,
    correctly downgrades a genuine contradiction to needs-review without
    discarding its underlying match signal, enforces case isolation, and
    is fully deterministic/replay-stable. The tested alternative
    configuration variant (halved weights, zero vector weight) changes
    only the numeric scores, not which candidates are retrieved or their
    relative ranking -- no tested alternative improves the baseline
    safely, so the existing profile is retained and frozen exactly as
    `RulesProfile`-wrapped in `scoring.BASELINE_RULES_PROFILE`, not
    replaced by the alternative used only for comparison here."""
    assert BASELINE_RULES_PROFILE.version == "phase5_preliminary_rules_v1"
    assert BASELINE_RULES_PROFILE.weights["exact_identifier"] == 60.0
