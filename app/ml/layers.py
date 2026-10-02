"""The six scoring layers, each emitting one score per transaction.

Every layer answers a different question at its own natural grain and is then
projected onto the transaction, which is the grain the labels live at:

    A  structure   transaction        equal-output shape, peel geometry, batch
    B  latency     outpoint  -> tx    rapid-spend behaviour, as survival
    C  history     entity x window    surge, recurrence, reactivation, baseline
    D  population  motif series       sudden surge in CoinJoin and peeling
    E  graph       transaction        bounded neighbourhood + PS network context
    F  fusion      transaction        see `app.ml.fusion`

Fits happen on `train_reference` rows only.  A layer never sees a label.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.cluster import MiniBatchKMeans
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from app.ml.detectors import (
    ECOD,
    HBOS,
    LatencySurvival,
    bocpd_run_length,
    empirical_bayes_rate,
    poisson_ewma_burst,
    poisson_upper_surprisal,
)
from app.ml.facts import Facts
from app.ml.grains import (
    HistoryTable,
    LatencyTable,
    Table,
    build_motif_series,
)

SEED = 42


@dataclass
class LayerScore:
    """One layer's output plus whatever a reviewer needs to read it."""

    name: str
    score: np.ndarray                      # per transaction, higher = more anomalous
    detail: dict[str, np.ndarray] = field(default_factory=dict)
    attribution: np.ndarray | None = None  # per-row, per-feature contributions
    attribution_columns: tuple[str, ...] = ()
    notes: dict[str, object] = field(default_factory=dict)


def _rank_normalise(values: np.ndarray, reference: np.ndarray | None = None) -> np.ndarray:
    """Map to [0, 1] against the reference distribution, so layers can be averaged.

    With `reference`, this is the reference split's ECDF applied to every row —
    the same transform a deployed scorer can apply to one transaction at a time.
    Ranking the full array against itself is monotone and so does not change any
    within-split ordering, but it is transductive: it needs the whole dataset in
    hand. Fitting on reference rows keeps the operational and offline paths
    identical.
    """
    values = np.asarray(values, dtype=np.float64)
    if reference is None or not reference.any():
        order = np.argsort(values, kind="stable")
        ranks = np.empty(values.shape[0], dtype=np.float64)
        ranks[order] = np.arange(values.shape[0], dtype=np.float64)
        return ranks / max(values.shape[0] - 1, 1)
    baseline = np.sort(values[reference])
    return np.searchsorted(baseline, values, side="right") / max(baseline.shape[0], 1)


# --------------------------------------------------------------------------- #
# Layer A — transaction structure
# --------------------------------------------------------------------------- #
def layer_a_structure(
    table: Table,
    train_mask: np.ndarray,
    *,
    n_clusters: int = 8,
    stratified: bool = True,
    n_jobs: int = 1,
    score_with: tuple[str, ...] = ("isolation_forest", "ecod"),
) -> LayerScore:
    """Isolation Forest + ECOD, optionally scored *within* a shape family.

    `score_with` names the detectors whose ranks form the layer score. ECOD is
    always fitted, because its per-column contributions are the explanation a
    reviewer sees; release anomaly-stack-v2 ranks by Isolation Forest alone
    (it beat IF+ECOD on every task, split and dataset in the v2 study,
    experiments/model_decision_v2.md).

    Two detectors rather than one: Isolation Forest ranks well but its score is a
    path length with no per-feature meaning, while ECOD's score is literally a sum
    of per-column tail log-probabilities.  Rank-averaging keeps the ranking and
    gains the explanation.

    Stratification is the part that matters most on real data.  Scored globally,
    the largest and most unusual *benign* shapes — exchange batch fan-outs, big
    consolidations — sit permanently at the top and bury everything else.  Scoring
    each transaction against its own shape family asks the useful question: is
    this transaction odd *for something of its kind*?
    """
    matrix = table.matrix.astype(np.float64)
    scaler = StandardScaler().fit(matrix[train_mask])
    scaled = scaler.transform(matrix)

    if stratified:
        kmeans = MiniBatchKMeans(
            n_clusters=n_clusters, random_state=SEED, n_init=5, batch_size=4096
        ).fit(scaled[train_mask])
        family = kmeans.predict(scaled).astype(np.int32)
    else:
        family = np.zeros(matrix.shape[0], dtype=np.int32)

    forest_score = np.zeros(matrix.shape[0], dtype=np.float64)
    ecod_score = np.zeros(matrix.shape[0], dtype=np.float64)
    attribution = np.zeros(matrix.shape, dtype=np.float32)

    for label in np.unique(family):
        members = family == label
        fit_rows = members & train_mask
        # A family too small to fit its own model falls back to the global
        # reference rows rather than being silently left unscored.
        reference = scaled[fit_rows] if fit_rows.sum() >= 64 else scaled[train_mask]
        forest = IsolationForest(
            n_estimators=100, max_samples=min(256, reference.shape[0]),
            random_state=SEED, n_jobs=n_jobs,
        ).fit(reference)
        forest_score[members] = -forest.score_samples(scaled[members])
        ecod = ECOD().fit(reference)
        contributions = ecod.contributions(scaled[members])
        ecod_score[members] = contributions.sum(axis=1)
        attribution[members] = contributions.astype(np.float32)

    ranked = {"isolation_forest": forest_score, "ecod": ecod_score}
    unknown = set(score_with) - set(ranked)
    if unknown or not score_with:
        raise ValueError(f"score_with must name isolation_forest and/or ecod, got {score_with}")
    combined = np.mean([_rank_normalise(ranked[name], train_mask) for name in score_with], axis=0)
    return LayerScore(
        name="A_structure",
        score=combined,
        detail={"isolation_forest": forest_score, "ecod": ecod_score, "shape_family": family},
        attribution=attribution,
        attribution_columns=table.columns,
        notes={"stratified": stratified, "clusters": int(np.unique(family).size), "score_with": list(score_with)},
    )


def layer_a_hbos(table: Table, train_mask: np.ndarray) -> LayerScore:
    """Optional third opinion on the same grain; assumes feature independence."""
    matrix = table.matrix.astype(np.float64)
    scaler = StandardScaler().fit(matrix[train_mask])
    scaled = scaler.transform(matrix)
    score = HBOS().fit(scaled[train_mask]).score(scaled)
    return LayerScore(name="A_hbos", score=_rank_normalise(score, train_mask))


# --------------------------------------------------------------------------- #
# Layer B — spend latency as survival
# --------------------------------------------------------------------------- #
def layer_b_latency(
    latency: LatencyTable,
    facts: Facts,
    train_mask_tx: np.ndarray,
    *,
    reference_latency: LatencyTable | None = None,
) -> LayerScore:
    """Score each spend by how fast it was for an output of its value.

    Two separate causality controls:

    * the curve is fitted on outputs **created** in the reference split, and
    * on a `reference_latency` table built with the reference period's end as its
      censoring horizon, so a spend that happened after training ends is censored
      rather than observed.  Without that second control the survival curve is
      shaped by events from validation and holdout, which is a direct leak into
      the baseline every later row is scored against.

    Censored outputs contribute to the risk set but never to an event.
    """
    source = reference_latency if reference_latency is not None else latency
    fit_rows = train_mask_tx[source.created_tx]
    model = LatencySurvival(strata=10).fit(
        source.values[fit_rows], source.duration[fit_rows], source.observed[fit_rows]
    )
    surprisal = np.zeros(latency.duration.shape[0], dtype=np.float64)
    observed = latency.observed
    surprisal[observed] = model.surprisal(latency.values[observed], latency.duration[observed])

    # Project onto the spending transaction: a transaction is as interesting as
    # its most surprisingly-fast input.
    per_tx = np.zeros(facts.transaction_count, dtype=np.float64)
    spending = latency.spending_tx[observed]
    np.maximum.at(per_tx, spending, surprisal[observed])
    return LayerScore(
        name="B_latency",
        score=_rank_normalise(per_tx, train_mask_tx),
        detail={"outpoint_surprisal": surprisal},
        notes={
            "observed_spends": int(observed.sum()),
            "censored_outputs": int((~observed).sum()),
            "fitted_on_reference_horizon": reference_latency is not None,
            "fit_events": int(source.observed[fit_rows].sum()),
        },
    )


# --------------------------------------------------------------------------- #
# Layer C — causal per-entity baseline and surge
# --------------------------------------------------------------------------- #
def layer_c_history(
    history: HistoryTable,
    facts: Facts,
    train_mask_tx: np.ndarray,
    *,
    shrinkage: float = 4.0,
) -> LayerScore:
    """Poisson surprisal against an empirical-Bayes-shrunk causal baseline.

    Four signals are combined, each computed only from windows strictly earlier
    than the row's own:

    * activity surge  -- -log P(count >= k | shrunk rate)
    * value surge     -- robust z on log value against a running median/MAD proxy
    * reactivation    -- gap since this address was last seen, against its own mean
    * motif recurrence-- how often this address showed a motif before

    Shrinkage is what makes the first sighting of an address scoreable at all.
    Without it 50%+ of rows here (and 97% on the old fixture) have no baseline and
    fall back to a neutral constant that carries no information.
    """
    rows = history.address.shape[0]
    train_rows = np.array(
        [bool(train_mask_tx[block[0]]) if block.size else False for block in history.transactions]
    )
    population_events = float(history.event_count[train_rows].mean()) if train_rows.any() else 1.0
    population_value = float(np.log1p(history.value_total[train_rows]).mean()) if train_rows.any() else 0.0

    rate = empirical_bayes_rate(history.prior_events, history.prior_windows, population_events, shrinkage)
    activity = poisson_upper_surprisal(history.event_count, rate)

    log_value = np.log1p(history.value_total)
    prior_mean_log = np.where(
        history.prior_windows > 0,
        np.log1p(history.prior_value / np.maximum(history.prior_windows, 1)),
        population_value,
    )
    spread = float(np.std(log_value[train_rows])) if train_rows.any() else 1.0
    value_surge = np.maximum(log_value - prior_mean_log, 0.0) / max(spread, 1e-6)

    mean_gap = np.where(
        history.prior_windows > 1,
        (history.window - history.window.min()) / np.maximum(history.prior_windows, 1),
        np.nan,
    )
    reactivation = np.zeros(rows, dtype=np.float64)
    seen = (history.gap_seconds > 0) & np.isfinite(mean_gap) & (mean_gap > 0)
    reactivation[seen] = np.log1p(history.gap_seconds[seen] / mean_gap[seen])

    recurrence = np.log1p(history.prior_motif)

    row_score = (
        _rank_normalise(activity, train_rows)
        + _rank_normalise(value_surge, train_rows)
        + 0.5 * _rank_normalise(reactivation, train_rows)
        + 0.5 * _rank_normalise(recurrence, train_rows)
    ) / 3.0

    # Project onto transactions: a transaction inherits the strongest history
    # signal of any address it paid into during that window.
    per_tx = np.zeros(facts.transaction_count, dtype=np.float64)
    for slot in range(rows):
        block = history.transactions[slot]
        if block.size:
            np.maximum.at(per_tx, block, row_score[slot])
    return LayerScore(
        name="C_history",
        score=_rank_normalise(per_tx, train_mask_tx),
        detail={
            "activity_surprisal": activity,
            "value_surge": value_surge,
            "reactivation": reactivation,
            "motif_recurrence": recurrence,
            "shrunk_rate": rate,
        },
        notes={
            "rows": int(rows),
            "rows_with_prior_history": int((history.prior_windows > 0).sum()),
            "shrinkage": shrinkage,
        },
    )


# --------------------------------------------------------------------------- #
# Layer D — population motif burst
# --------------------------------------------------------------------------- #
def layer_d_burst(
    facts: Facts,
    family_flags: dict[str, np.ndarray],
    *,
    bucket_seconds: int = 900,
    half_life: float = 96.0,
    with_bocpd: bool = True,
    reference: np.ndarray | None = None,
) -> LayerScore:
    """Is there more of this motif right now than there should be?

    One count series per motif family, a Poisson EWMA control chart over each,
    and optionally BOCPD for a change-point posterior a reviewer can be shown.
    Every transaction inherits the burst score of its own bucket and family.
    """
    per_tx = np.zeros(facts.transaction_count, dtype=np.float64)
    detail: dict[str, np.ndarray] = {}
    notes: dict[str, object] = {"families": sorted(family_flags), "bucket_seconds": bucket_seconds}

    for family, flag in family_flags.items():
        bucket_start, counts, bucket_of_tx = build_motif_series(facts, flag, bucket_seconds=bucket_seconds)
        burst = poisson_ewma_burst(counts, half_life=half_life)
        detail[f"{family}_counts"] = counts
        detail[f"{family}_burst"] = burst
        detail[f"{family}_bucket_start"] = bucket_start
        if with_bocpd:
            posterior = bocpd_run_length(counts)
            detail[f"{family}_changepoint_posterior"] = posterior
            notes[f"{family}_changepoints"] = int((posterior > 0.5).sum())
        # Only transactions of this family carry its burst score; an ordinary
        # transaction inside a CoinJoin surge is not itself a CoinJoin.
        members = flag > 0
        per_tx[members] = np.maximum(per_tx[members], burst[bucket_of_tx[members]])
    return LayerScore(name="D_burst", score=_rank_normalise(per_tx, reference), detail=detail, notes=notes)


# --------------------------------------------------------------------------- #
# Layer E — bounded graph neighbourhood
# --------------------------------------------------------------------------- #
def layer_e_graph(table: Table, train_mask: np.ndarray, *, n_jobs: int = 1) -> LayerScore:
    """Isolation Forest over bounded k<=2 graph and PS network-context features."""
    matrix = table.matrix.astype(np.float64)
    scaler = StandardScaler().fit(matrix[train_mask])
    scaled = scaler.transform(matrix)
    forest = IsolationForest(
        n_estimators=100, max_samples=min(256, int(train_mask.sum())), random_state=SEED, n_jobs=n_jobs
    ).fit(scaled[train_mask])
    score = -forest.score_samples(scaled)
    ecod = ECOD().fit(scaled[train_mask])
    contributions = ecod.contributions(scaled)
    combined = 0.5 * (
        _rank_normalise(score, train_mask) + _rank_normalise(contributions.sum(axis=1), train_mask)
    )
    return LayerScore(
        name="E_graph",
        score=combined,
        attribution=contributions.astype(np.float32),
        attribution_columns=table.columns,
    )


# --------------------------------------------------------------------------- #
# Rule baseline — the required comparator
# --------------------------------------------------------------------------- #
def rule_baseline(facts: Facts, table: Table) -> LayerScore:
    """The existing deterministic Phase 4.1 predicates, as a score.

    Reimplemented over the same arrays rather than called through the database
    path, but with the same thresholds: at least three inputs, three outputs and
    three exactly-equal outputs for the equal-output shape; a 2-output
    transaction whose continuation dominates for a peel step.  This is the
    comparator every candidate has to beat, and it is deliberately the rule as
    specified, not a strengthened version of it.
    """
    columns = {name: index for index, name in enumerate(table.columns)}
    matrix = table.matrix
    equal = matrix[:, columns["equal_group_exact"]]
    n_in = matrix[:, columns["n_inputs"]]
    n_out = matrix[:, columns["n_outputs"]]
    peel = matrix[:, columns["one_big_one_small"]]

    coinjoin_like = (n_in >= 3) & (n_out >= 3) & (equal >= 3)
    peel_like = peel > 0
    score = np.zeros(facts.transaction_count, dtype=np.float64)
    # Rank ties are broken by equal-group size then by fan-out, exactly as the
    # deterministic rule's own priority score does.
    score[peel_like] = 0.5
    score[coinjoin_like] = 1.0 + equal[coinjoin_like] / max(equal.max(), 1.0)
    return LayerScore(
        name="rule_baseline",
        score=score,
        notes={"coinjoin_like_flagged": int(coinjoin_like.sum()), "peel_like_flagged": int(peel_like.sum())},
    )


def motif_family_flags(facts: Facts, table: Table) -> dict[str, np.ndarray]:
    """Family membership for layer D's count series, from structure alone.

    Derived from the deterministic predicates, never from `evaluation_truth.json`.
    Layer D must be able to run on data that has no labels at all.
    """
    columns = {name: index for index, name in enumerate(table.columns)}
    matrix = table.matrix
    equal = matrix[:, columns["equal_group_tol_100"]]
    n_in = matrix[:, columns["n_inputs"]]
    n_out = matrix[:, columns["n_outputs"]]
    return {
        "coinjoin_like": ((n_in >= 3) & (n_out >= 3) & (equal >= 3)).astype(np.float64),
        "peel_step": (matrix[:, columns["one_big_one_small"]] > 0).astype(np.float64),
    }


# --------------------------------------------------------------------------- #
# Layer S — supervised ranker (optional, and only honest with real labels)
# --------------------------------------------------------------------------- #
def layer_s_supervised(
    tables: list[Table],
    extra_scores: dict[str, np.ndarray],
    train_mask: np.ndarray,
    positive: np.ndarray,
    *,
    sample_mask: np.ndarray | None = None,
    n_jobs: int = 1,
) -> LayerScore:
    """Gradient-boosted classifier over every grain's features at once.

    This is the one layer that reads labels, and what it is worth depends
    entirely on where those labels came from:

    * On the *motif* task the fixture's labels are the generator's own shape
      families, which is very close to the deterministic rule's predicate.
      Training on them is close to circular and a high score there proves little.
    * On the *discrimination* task it is a real question: `nearmiss_rule_positive`
      transactions satisfy the rule exactly, so the rule cannot separate them by
      construction.  Anything the model gains there is gain the rule cannot have.

    In production the non-circular label source is the reviewer decisions already
    recorded through `POST /v1/findings/{id}/reviews` — analyst confirm/dismiss,
    not synthetic truth.  Treat this layer as the harness for that, measured here
    on a fixture standing in for it.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    blocks = [table.matrix.astype(np.float32) for table in tables]
    columns: list[str] = [name for table in tables for name in table.columns]
    for name in sorted(extra_scores):
        blocks.append(np.asarray(extra_scores[name], dtype=np.float32).reshape(-1, 1))
        columns.append(f"score::{name}")
    matrix = np.hstack(blocks)

    fit_rows = train_mask if sample_mask is None else (train_mask & sample_mask)
    if fit_rows.sum() < 50 or positive[fit_rows].sum() < 10:
        # Refuse rather than return a model fitted on almost nothing.
        return LayerScore(
            name="S_supervised",
            score=np.zeros(matrix.shape[0], dtype=np.float64),
            notes={"trained": False, "reason": "not enough labelled reference rows"},
        )
    model = HistGradientBoostingClassifier(
        random_state=SEED, max_iter=200, learning_rate=0.08,
        max_leaf_nodes=31, l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
    ).fit(matrix[fit_rows], positive[fit_rows].astype(np.int8))
    probability = model.predict_proba(matrix)[:, 1]
    return LayerScore(
        name="S_supervised",
        score=probability,
        attribution_columns=tuple(columns),
        notes={
            "trained": True,
            "train_rows": int(fit_rows.sum()),
            "train_positives": int(positive[fit_rows].sum()),
            "features": len(columns),
            "label_source": "fixture evaluation truth (stand-in for analyst review decisions)",
        },
    )
