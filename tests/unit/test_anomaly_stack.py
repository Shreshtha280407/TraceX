"""Correctness tests for the anomaly stack.

Three things matter more than any metric here and are tested directly:

* **causality** — a history feature must use only strictly-earlier windows;
* **leakage** — no label, address, txid or future value may reach a `fit()`;
* **determinism** — the same seed and inputs must produce the same ranking, or
  no comparison between candidates means anything.

The detectors are tested against constructed inputs with known answers rather
than against fixture output, so a failure points at the arithmetic.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.ml import detectors, evaluate, fusion, grains, layers
from app.ml.facts import Facts, assign_splits
from app.ml.facts import truncate_facts as facts_truncate


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
def test_ecod_scores_tails_above_the_bulk_and_decomposes() -> None:
    rng = np.random.default_rng(0)
    reference = rng.normal(size=(2000, 4))
    model = detectors.ECOD().fit(reference)

    probe = np.vstack([np.zeros((1, 4)), np.full((1, 4), 8.0)])
    scores = model.score(probe)
    assert scores[1] > scores[0], "an extreme point must outscore the centre"

    contributions = model.contributions(probe)
    # The explanation is the score: rows must sum exactly to what `score` returns.
    assert np.allclose(contributions.sum(axis=1), scores)
    assert contributions.shape == probe.shape


def test_ecod_never_returns_infinity_on_unseen_extremes() -> None:
    model = detectors.ECOD().fit(np.arange(100, dtype=float).reshape(-1, 1))
    score = model.score(np.array([[1e9]]))
    assert np.isfinite(score).all()


def test_hbos_ranks_a_sparse_bin_above_a_dense_one() -> None:
    values = np.concatenate([np.zeros(500), np.array([50.0])]).reshape(-1, 1)
    model = detectors.HBOS(bins=10).fit(values)
    scores = model.score(values)
    assert scores[-1] > scores[0]


def test_poisson_surprisal_grows_with_the_excess() -> None:
    rates = np.full(3, 2.0)
    surprisal = detectors.poisson_upper_surprisal(np.array([2.0, 6.0, 20.0]), rates)
    assert surprisal[0] < surprisal[1] < surprisal[2]
    assert np.isfinite(surprisal).all()


def test_empirical_bayes_interpolates_between_prior_and_own_history() -> None:
    """The cold-start fix: with no history the rate *is* the population prior, and
    it converges on the entity's own mean as windows accumulate."""
    cold = detectors.empirical_bayes_rate(np.array([0.0]), np.array([0.0]), 5.0, 4.0)
    assert cold[0] == pytest.approx(5.0)

    warm = detectors.empirical_bayes_rate(np.array([100.0]), np.array([100.0]), 5.0, 4.0)
    assert warm[0] == pytest.approx(1.0, abs=0.2)


def test_poisson_ewma_burst_fires_on_a_step_change_and_is_causal() -> None:
    counts = np.concatenate([np.full(200, 1.0), np.full(20, 30.0)])
    burst = detectors.poisson_ewma_burst(counts, half_life=20.0)
    assert burst[:200].max() < burst[200:].max()
    # The baseline at the first surge step may only use steps before it, so the
    # very first surge bucket must already score high rather than be absorbed.
    assert burst[200] > np.quantile(burst[:200], 0.99)


def test_bocpd_posterior_peaks_near_the_change_point() -> None:
    counts = np.concatenate([np.full(120, 1.0), np.full(120, 25.0)])
    posterior = detectors.bocpd_run_length(counts, hazard=1 / 100.0, max_run=150)
    assert posterior.shape == counts.shape
    assert 0.0 <= posterior.min() and posterior.max() <= 1.0
    assert 115 <= int(np.argmax(posterior[100:])) + 100 <= 130


def test_latency_survival_ranks_a_fast_spend_above_a_typical_one() -> None:
    rng = np.random.default_rng(3)
    values = rng.uniform(1e5, 1e7, size=4000)
    durations = rng.lognormal(mean=11.0, sigma=1.0, size=4000)
    observed = rng.random(4000) < 0.7
    model = detectors.LatencySurvival(strata=5).fit(values, durations, observed)

    probe_values = np.array([1e6, 1e6])
    probe_durations = np.array([30.0, 1e5])
    surprisal = model.surprisal(probe_values, probe_durations)
    assert surprisal[0] > surprisal[1], "a 30-second spend must outscore a typical one"


def test_kaplan_meier_treats_censored_rows_as_at_risk_not_as_events() -> None:
    durations = np.array([1.0, 2.0, 3.0, 4.0])
    all_events = detectors._kaplan_meier(durations, np.ones(4, dtype=bool))
    mostly_censored = detectors._kaplan_meier(durations, np.array([True, False, False, False]))
    # With three of four censored, survival must stay far higher.
    assert mostly_censored[1][-1] > all_events[1][-1]


# --------------------------------------------------------------------------- #
# Grains
# --------------------------------------------------------------------------- #
def _toy_facts() -> Facts:
    """Four transactions: a funder, an equal-output shape, a peel, a plain spend."""
    txids = [f"tx{index}" for index in range(4)]
    out_tx = np.array([0, 0, 0, 1, 1, 1, 1, 2, 2, 3], dtype=np.int32)
    out_vout = np.array([0, 1, 2, 0, 1, 2, 3, 0, 1, 0], dtype=np.int32)
    out_value = np.array([1000, 1000, 1000, 500, 500, 500, 490, 900, 90, 980], dtype=np.int64)
    out_addr = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], dtype=np.int32)
    return Facts(
        txids=txids,
        tx_index={txid: index for index, txid in enumerate(txids)},
        tx_time=np.array([0, 100, 200, 5000], dtype=np.int64),
        tx_fee=np.array([0, 10, 10, 10], dtype=np.int64),
        out_tx=out_tx, out_vout=out_vout, out_value=out_value, out_addr=out_addr,
        out_script=np.zeros(10, dtype=np.int8),
        out_spent_by=np.array([1, 1, 1, 2, -1, -1, -1, 3, -1, -1], dtype=np.int32),
        in_tx=np.array([0, 1, 1, 1, 2, 3], dtype=np.int32),
        in_prev=np.array([-1, 0, 1, 2, 3, 7], dtype=np.int32),
        addresses=[f"addr{index}" for index in range(10)],
        addr_index={f"addr{index}": index for index in range(10)},
    )


def test_transaction_table_sees_the_equal_output_shape() -> None:
    table = grains.build_transaction_table(_toy_facts())
    columns = {name: index for index, name in enumerate(table.columns)}
    # tx1 has outputs 500/500/500/490 -> three exactly equal, four within 10 sats.
    assert table.matrix[1, columns["equal_group_exact"]] == 3
    assert table.matrix[1, columns["equal_group_tol_10"]] == 4
    assert table.matrix[1, columns["n_inputs"]] == 3
    # tx2 is 900/90 -> the peel shape; tx3 is a single output and is not.
    assert table.matrix[2, columns["one_big_one_small"]] == 1
    assert table.matrix[3, columns["one_big_one_small"]] == 0


def test_graded_equal_group_ranks_a_near_miss_below_an_exact_match() -> None:
    exact = grains._largest_equal_group(np.array([500, 500, 500, 490]), 0)
    near = grains._largest_equal_group(np.array([500, 499, 501, 490]), 0)
    assert exact == 3 and near == 1
    # ...but the graded tolerance still sees the near miss, at a lower rank.
    assert grains._largest_equal_group(np.array([500, 499, 501, 490]), 10) == 3


def test_history_baselines_use_only_strictly_earlier_windows() -> None:
    """The single most important property of layer C.

    One address receiving in three successive windows must carry a baseline that
    never includes its own current window, or the surge signal is circular.
    """
    facts = _toy_facts()
    facts.out_addr = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=np.int32)
    facts.tx_time = np.array([0, 1000, 2000, 3000], dtype=np.int64)
    history = grains.build_history_table(facts)

    assert history.prior_windows[0] == 0, "the first window must have no baseline"
    assert history.prior_events[0] == 0
    for slot in range(1, history.address.shape[0]):
        assert history.prior_windows[slot] == slot
        # The running event total may only cover earlier rows.
        assert history.prior_events[slot] == history.event_count[:slot].sum()
        assert history.prior_value[slot] == history.value_total[:slot].sum()


def test_history_resets_between_addresses() -> None:
    facts = _toy_facts()
    facts.out_addr = np.array([0, 0, 1, 1, 1, 1, 1, 1, 1, 1], dtype=np.int32)
    history = grains.build_history_table(facts)
    first_of_each = [0] + [slot for slot in range(1, history.address.shape[0])
                           if history.address[slot] != history.address[slot - 1]]
    for slot in first_of_each:
        assert history.prior_windows[slot] == 0, "history leaked across addresses"


def test_motif_series_counts_only_flagged_transactions() -> None:
    facts = _toy_facts()
    flag = np.array([0.0, 1.0, 1.0, 0.0])
    _, counts, bucket = grains.build_motif_series(facts, flag, bucket_seconds=900)
    assert counts.sum() == 2
    assert counts[bucket[1]] >= 1


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def test_fusion_calibrates_on_reference_rows_only() -> None:
    rng = np.random.default_rng(7)
    score = rng.normal(size=1000)
    reference = np.zeros(1000, dtype=bool)
    reference[:700] = True

    fused = fusion.stouffer_fuse({"a": score}, reference, budget=0.05)
    flagged_reference = fused.flagged[reference].mean()
    assert flagged_reference == pytest.approx(0.05, abs=0.02), (
        "the budget must hold on the rows the threshold was fitted to"
    )


def test_fusion_requires_a_declared_weight_for_every_layer() -> None:
    reference = np.ones(10, dtype=bool)
    with pytest.raises(ValueError, match="no fusion weight"):
        fusion.stouffer_fuse(
            {"a": np.zeros(10), "b": np.zeros(10)}, reference, weights={"a": 1.0}
        )


def test_fusion_of_one_layer_preserves_its_ranking() -> None:
    rng = np.random.default_rng(11)
    score = rng.normal(size=500)
    reference = np.ones(500, dtype=bool)
    fused = fusion.stouffer_fuse({"only": score}, reference)
    assert np.array_equal(np.argsort(fused.score), np.argsort(score))


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def test_average_precision_is_tie_aware() -> None:
    """The rule baseline emits three distinct scores.  Without tie handling its
    average precision would depend on array order, which is meaningless."""
    score = np.array([1.0, 1.0, 1.0, 1.0])
    positive_first = np.array([True, True, False, False])
    positive_last = np.array([False, False, True, True])
    assert evaluate._average_precision(score, positive_first) == pytest.approx(
        evaluate._average_precision(score, positive_last)
    )


def test_average_precision_rewards_a_correct_ranking() -> None:
    positive = np.array([True, False, True, False])
    good = np.array([1.0, 0.1, 0.9, 0.2])
    bad = np.array([0.1, 1.0, 0.2, 0.9])
    assert evaluate._average_precision(good, positive) > evaluate._average_precision(bad, positive)


def test_roc_auc_matches_sklearn() -> None:
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(5)
    score = rng.normal(size=400)
    positive = rng.random(400) < 0.3
    assert evaluate._roc_auc(score, positive) == pytest.approx(roc_auc_score(positive, score), abs=1e-9)


def test_discrimination_task_restricts_the_population_to_motifs_and_near_misses() -> None:
    labels = evaluate.Labels(
        positive=np.array([True, False, False, False]),
        near_miss=np.array([False, True, False, False]),
        family=np.array([0, 1, -1, -1], dtype=np.int8),
        families=("coinjoin_like", "nearmiss_rule_positive"),
        in_surge=np.array([True, False, False, False]),
    )
    positive, population = evaluate.task_targets(labels, "discrimination")
    assert population.sum() == 2, "unlabelled traffic must not dilute the discrimination task"
    assert positive[0] and not positive[1]


def test_layer_combinations_are_exhaustive_and_ordered() -> None:
    combinations = evaluate.layer_combinations(["A", "B", "C"])
    assert len(combinations) == 7
    assert combinations[0] == ("A",)
    assert combinations[-1] == ("A", "B", "C")


# --------------------------------------------------------------------------- #
# Leakage and determinism
# --------------------------------------------------------------------------- #
FORBIDDEN_SUBSTRINGS = (
    "address", "txid", "entity_ref", "case_id", "source_id", "locator", "scenario",
    "group_id", "seed_reason", "reviewer", "wallet", "label", "family_truth",
    "split", "episode", "in_surge",
)


def test_no_feature_column_names_an_identifier_or_a_label() -> None:
    """A denylist, not merely an absence: a future column called `address_label`
    must fail this test rather than quietly become a model input."""
    for name in grains.TRANSACTION_COLUMNS + grains.GRAPH_COLUMNS:
        lowered = name.lower()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in lowered, f"feature column {name!r} contains {forbidden!r}"


def test_risk_propagation_fields_are_absent_from_every_grain() -> None:
    """`docs/phase5a_handoff.md` hard-excludes these at any phase."""
    hard_excluded = {
        "risk_propagation_score", "risk_seed_distance", "risk_path_evidence_count", "risk_seed_count",
    }
    assert hard_excluded.isdisjoint(set(grains.TRANSACTION_COLUMNS) | set(grains.GRAPH_COLUMNS))


def test_split_assignment_is_time_respecting() -> None:
    tx_time = np.array([0, 10, 20, 30, 40], dtype=np.int64)
    splits = assign_splits(tx_time, (20, 40))
    assert list(splits) == [0, 0, 1, 1, 2]
    # Monotone: a later transaction can never land in an earlier split.
    assert np.all(np.diff(splits) >= 0)


def test_layer_a_is_deterministic_under_a_fixed_seed() -> None:
    rng = np.random.default_rng(13)
    matrix = rng.normal(size=(600, len(grains.TRANSACTION_COLUMNS))).astype(np.float32)
    table = grains.Table(grains.TRANSACTION_COLUMNS, matrix)
    train = np.zeros(600, dtype=bool)
    train[:400] = True

    first = layers.layer_a_structure(table, train, n_clusters=4)
    second = layers.layer_a_structure(table, train, n_clusters=4)
    assert np.array_equal(first.score, second.score), "a candidate comparison needs a stable ranking"


def test_rule_baseline_implements_the_documented_predicate() -> None:
    facts = _toy_facts()
    table = grains.build_transaction_table(facts)
    baseline = layers.rule_baseline(facts, table)
    # tx1: 3 inputs, 4 outputs, 3 exactly equal -> the equal-output predicate.
    assert baseline.score[1] > 1.0
    # tx2: the peel shape, scored below it.
    assert 0 < baseline.score[2] < 1.0
    # tx3: a single output, flagged by neither.
    assert baseline.score[3] == 0.0


# --------------------------------------------------------------------------- #
# Causality, proved by truncation rather than by inspection
# --------------------------------------------------------------------------- #
def _causal_facts() -> Facts:
    """Eight transactions over four distinct timestamps, with real spend chains.

    Deliberately includes an output created early and spent late, which is the
    exact shape every removed leak depended on.
    """
    txids = [f"tx{index}" for index in range(8)]
    out_tx = np.array([0, 0, 1, 1, 2, 3, 3, 4, 5, 6, 7], dtype=np.int32)
    out_vout = np.array([0, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0], dtype=np.int32)
    out_value = np.array([900, 100, 500, 500, 700, 300, 300, 250, 640, 610, 580], dtype=np.int64)
    # Addresses repeat so the prior-reuse features have something to count.
    out_addr = np.array([0, 1, 2, 2, 3, 0, 4, 5, 1, 2, 0], dtype=np.int32)
    # tx2 spends tx0:0 early; tx6 spends tx1:0 much later; tx0:1 is never spent.
    out_spent_by = np.array([2, -1, 6, -1, 5, 7, -1, -1, -1, -1, -1], dtype=np.int32)
    return Facts(
        txids=txids,
        tx_index={txid: index for index, txid in enumerate(txids)},
        tx_time=np.array([0, 100, 200, 300, 1000, 1100, 5000, 5100], dtype=np.int64),
        tx_fee=np.array([10, 10, 10, 10, 10, 10, 10, 10], dtype=np.int64),
        out_tx=out_tx, out_vout=out_vout, out_value=out_value, out_addr=out_addr,
        out_script=np.zeros(11, dtype=np.int8), out_spent_by=out_spent_by,
        in_tx=np.array([0, 1, 2, 3, 4, 5, 6, 7], dtype=np.int32),
        in_prev=np.array([-1, -1, 0, -1, -1, 4, 2, 5], dtype=np.int32),
        addresses=[f"addr{index}" for index in range(6)],
        addr_index={f"addr{index}": index for index in range(6)},
        tx_asn=np.array([0, 0, 1, 0, 1, 1, 0, 0], dtype=np.int32),
        tx_country=np.array([0, 1, 0, 0, 1, 2, 0, 1], dtype=np.int32),
        tx_src_ip=np.array([0, 0, 1, 1, 0, 0, 1, 1], dtype=np.int32),
        asns=["AS1", "AS2"], countries=["US", "DE", "SG"], src_ips=["1.1.1.1", "2.2.2.2"],
    )


@pytest.mark.parametrize("as_of", [100, 300, 1100])
def test_transaction_features_are_identical_on_a_truncated_snapshot(as_of: int) -> None:
    """The property test that makes causality checkable instead of arguable.

    Rebuild grain A on a view of the snapshot as it looked at `as_of`. Every
    transaction at or before `as_of` must get bit-identical features, because none
    of them may depend on anything that happened later. A single future-reading
    column makes this fail.
    """
    facts = _causal_facts()
    full = grains.build_transaction_table(facts).matrix
    truncated = grains.build_transaction_table(facts_truncate(facts, as_of)).matrix
    kept = int((facts.tx_time <= as_of).sum())
    assert truncated.shape[0] == kept
    np.testing.assert_array_equal(full[:kept], truncated)


@pytest.mark.parametrize("as_of", [100, 300, 1100])
def test_graph_features_are_identical_on_a_truncated_snapshot(as_of: int) -> None:
    """Same property for the graph grain, which is where the worst leaks were:
    `distinct_next_tx`, `chain_depth_forward` and whole-dataset reuse totals."""
    facts = _causal_facts()
    full = grains.build_graph_table(facts).matrix
    truncated = grains.build_graph_table(facts_truncate(facts, as_of)).matrix
    kept = int((facts.tx_time <= as_of).sum())
    np.testing.assert_array_equal(full[:kept], truncated)


def test_no_feature_column_reads_a_future_outcome() -> None:
    """A name-level guard for the class of feature that has no causal version."""
    forbidden = ("spent_output_share", "distinct_next_tx", "chain_depth_forward", "next_", "future_")
    for name in grains.TRANSACTION_COLUMNS + grains.GRAPH_COLUMNS:
        for token in forbidden:
            assert token not in name, f"{name!r} names a future outcome"


def test_latency_horizon_censors_spends_after_it() -> None:
    """An output spent after the horizon must be censored, not observed.

    Fitting the survival curve on uncensored durations that run past the training
    period is a leak into the baseline every later row is scored against.
    """
    facts = _causal_facts()
    full = grains.build_latency_table(facts)
    early = grains.build_latency_table(facts, horizon=300)

    # tx1:0 (output index 2) is spent by tx6 at t=5000.
    assert full.observed[2] and full.spending_tx[2] == 6
    assert not early.observed[2], "a spend after the horizon must be right-censored"
    assert early.spending_tx[2] == -1
    assert early.duration[2] == 300 - facts.tx_time[1]
    # tx0:0 (output index 0) is spent by tx2 at t=200, before the horizon.
    assert early.observed[0] and early.spending_tx[0] == 2


def test_trailing_cooccurrence_excludes_the_present_and_the_future() -> None:
    key = np.array([0, 0, 0, 0], dtype=np.int32)
    time = np.array([0, 10, 20, 5000], dtype=np.int64)
    counts = np.expm1(grains._trailing_cooccurrence(key, time, window=100))
    # First row sees nothing; each later row sees only earlier rows inside the
    # window; the far-future row has fallen out of it entirely.
    np.testing.assert_allclose(counts, [0.0, 1.0, 2.0, 0.0])


def test_prior_occurrence_index_counts_only_earlier_rows() -> None:
    key = np.array([7, 7, 9, 7], dtype=np.int32)
    time = np.array([0, 10, 5, 20], dtype=np.int64)
    tiebreak = np.arange(4, dtype=np.int64)
    prior = grains._prior_occurrence_index(key, time, tiebreak)
    np.testing.assert_array_equal(prior, [0, 1, 0, 2])


def test_rank_normalisation_fitted_on_reference_is_applicable_row_by_row() -> None:
    """Scoring must not need the whole dataset in hand.

    With a reference mask the transform is the reference ECDF, so scoring one
    transaction gives the same number as scoring it inside a batch.
    """
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    reference = np.array([True, True, True, False, False])
    batch = layers._rank_normalise(values, reference)
    single = np.array([layers._rank_normalise(np.array([v]), None) * 0 + layers._rank_normalise(
        np.concatenate([values[reference], [v]]),
        np.append(reference[reference], False),
    )[-1] for v in values]).ravel()
    np.testing.assert_allclose(batch, single)
