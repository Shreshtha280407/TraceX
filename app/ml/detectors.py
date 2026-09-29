"""Detectors written in numpy so the stack needs no dependency beyond scikit-learn.

`pyod` would supply ECOD and HBOS, but each is ~40 lines and keeping them in-tree
means a reviewer can read the exact arithmetic behind a score.  That matters here:
the PS asks for *explainable* ranked findings, and ECOD's per-dimension tail
probabilities are the explanation.
"""

from __future__ import annotations

import numpy as np

EPSILON = 1e-12


# --------------------------------------------------------------------------- #
# ECOD — Empirical Cumulative distribution based Outlier Detection
# --------------------------------------------------------------------------- #
class ECOD:
    """Per-dimension empirical tail probabilities, summed in log space.

    For every column the left tail P(X <= x) and right tail P(X >= x) are read
    off the training ECDF.  A point's score is the sum over columns of
    -log(tail), so the score decomposes exactly into per-feature contributions —
    which is what makes a finding explainable rather than just ranked.

    Skewness decides which tail a column contributes, following the published
    method: right-skewed columns are judged by their right tail, left-skewed by
    their left, and the symmetric aggregate is kept as a third candidate.
    """

    def __init__(self) -> None:
        self._sorted: list[np.ndarray] = []
        self._skew: np.ndarray | None = None
        self._n = 0

    def fit(self, matrix: np.ndarray) -> ECOD:
        matrix = np.asarray(matrix, dtype=np.float64)
        self._n = matrix.shape[0]
        self._sorted = [np.sort(matrix[:, column]) for column in range(matrix.shape[1])]
        mean = matrix.mean(axis=0)
        centred = matrix - mean
        variance = np.maximum((centred ** 2).mean(axis=0), EPSILON)
        self._skew = (centred ** 3).mean(axis=0) / variance ** 1.5
        return self

    def _tails(self, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        left = np.empty(matrix.shape, dtype=np.float64)
        right = np.empty(matrix.shape, dtype=np.float64)
        for column, reference in enumerate(self._sorted):
            values = matrix[:, column]
            below = np.searchsorted(reference, values, side="right")
            above = self._n - np.searchsorted(reference, values, side="left")
            # Clamp at one observation so a column can never contribute -log(0).
            left[:, column] = np.maximum(below, 1) / self._n
            right[:, column] = np.maximum(above, 1) / self._n
        return left, right

    def contributions(self, matrix: np.ndarray) -> np.ndarray:
        """Per-row, per-column -log(tail) contributions; rows sum to the score."""
        matrix = np.asarray(matrix, dtype=np.float64)
        left, right = self._tails(matrix)
        assert self._skew is not None
        skewed = np.where(self._skew > 0, right, left)
        symmetric = np.minimum(left, right)
        return -np.log(np.minimum(skewed, symmetric))

    def score(self, matrix: np.ndarray) -> np.ndarray:
        return self.contributions(matrix).sum(axis=1)


# --------------------------------------------------------------------------- #
# HBOS — Histogram Based Outlier Score
# --------------------------------------------------------------------------- #
class HBOS:
    """Independent per-column histogram densities, summed in log space.

    Cheap, and a useful third opinion precisely because it assumes independence:
    where it disagrees with Isolation Forest, the disagreement is about feature
    interaction rather than about the marginals.
    """

    def __init__(self, bins: int = 32) -> None:
        self._bins = bins
        self._edges: list[np.ndarray] = []
        self._log_density: list[np.ndarray] = []

    def fit(self, matrix: np.ndarray) -> HBOS:
        matrix = np.asarray(matrix, dtype=np.float64)
        rows = matrix.shape[0]
        for column in range(matrix.shape[1]):
            values = matrix[:, column]
            low, high = float(values.min()), float(values.max())
            if not np.isfinite(low) or not np.isfinite(high) or high <= low:
                edges = np.array([low - 0.5, low + 0.5])
            else:
                edges = np.linspace(low, high, self._bins + 1)
            counts, edges = np.histogram(values, bins=edges)
            density = np.maximum(counts, 1) / rows
            self._edges.append(edges)
            self._log_density.append(-np.log(density / density.max()))
        return self

    def score(self, matrix: np.ndarray) -> np.ndarray:
        matrix = np.asarray(matrix, dtype=np.float64)
        total = np.zeros(matrix.shape[0], dtype=np.float64)
        for column, (edges, log_density) in enumerate(zip(self._edges, self._log_density, strict=True)):
            slot = np.clip(np.searchsorted(edges, matrix[:, column], side="right") - 1, 0, len(log_density) - 1)
            total += log_density[slot]
        return total


# --------------------------------------------------------------------------- #
# Causal Poisson surprisal with empirical-Bayes shrinkage
# --------------------------------------------------------------------------- #
def poisson_upper_surprisal(counts: np.ndarray, rates: np.ndarray) -> np.ndarray:
    """-log P(X >= k | lambda) for a Poisson rate, computed stably.

    A plain ratio (`count / baseline`, which is what the current exporter's
    `activity_surge_ratio` computes) cannot say how *surprising* a jump is: 1 -> 3
    and 100 -> 300 give the same ratio and wildly different surprisal.
    """
    counts = np.asarray(counts, dtype=np.float64)
    rates = np.maximum(np.asarray(rates, dtype=np.float64), EPSILON)
    from scipy.stats import poisson

    tail = poisson.sf(counts - 1, rates)
    return -np.log(np.clip(tail, EPSILON, 1.0))


def empirical_bayes_rate(
    observed_total: np.ndarray,
    observed_windows: np.ndarray,
    population_mean: float,
    strength: float,
) -> np.ndarray:
    """Shrink each entity's own mean toward the population mean.

    This is the fix for cold start.  With no shrinkage an entity seen for the
    first time has no baseline at all, and the current exporter imputes a neutral
    1.0 that carries no information — which on the v1 fixture was 96.88% of rows.
    With shrinkage a first-seen entity gets the population prior, and its own
    history takes over smoothly as windows accumulate.
    """
    observed_total = np.asarray(observed_total, dtype=np.float64)
    observed_windows = np.asarray(observed_windows, dtype=np.float64)
    return (observed_total + strength * population_mean) / (observed_windows + strength)


# --------------------------------------------------------------------------- #
# Population burst detection
# --------------------------------------------------------------------------- #
def poisson_ewma_burst(counts: np.ndarray, half_life: float, min_rate: float = 0.25) -> np.ndarray:
    """Poisson EWMA control chart over a motif count series.

    The baseline at step t uses only steps < t, so the score at the moment a
    surge begins is not contaminated by the surge itself.
    """
    counts = np.asarray(counts, dtype=np.float64)
    decay = 0.5 ** (1.0 / max(half_life, EPSILON))
    baseline = np.empty_like(counts)
    running = float(counts[:8].mean()) if counts.size >= 8 else float(counts.mean() if counts.size else 0.0)
    for step, value in enumerate(counts):
        baseline[step] = max(running, min_rate)
        running = decay * running + (1.0 - decay) * value
    return poisson_upper_surprisal(counts, baseline)


def bocpd_run_length(counts: np.ndarray, hazard: float = 1.0 / 240.0, max_run: int = 400) -> np.ndarray:
    """Bayesian online change-point detection on a Poisson count series.

    Returns, per step, the posterior probability that a change point occurred —
    a quantity a reviewer can be shown directly ("change point at 14:15,
    posterior 0.91") rather than a bare anomaly score.

    Conjugate Gamma(alpha, beta) prior per run length; the predictive is negative
    binomial.  Kept O(max_run) per step so a 50,000-step series stays instant.
    """
    from scipy.stats import nbinom

    counts = np.asarray(counts, dtype=np.float64)
    alpha0, beta0 = 1.0, 1.0
    run_probability = np.zeros(max_run + 1)
    run_probability[0] = 1.0
    alpha = np.full(max_run + 1, alpha0)
    beta = np.full(max_run + 1, beta0)
    change = np.zeros(counts.shape[0])

    for step, value in enumerate(counts):
        live = min(step + 1, max_run + 1)
        predictive = nbinom.pmf(value, alpha[:live], beta[:live] / (beta[:live] + 1.0))
        predictive = np.clip(predictive, EPSILON, None)

        growth = run_probability[:live] * predictive * (1.0 - hazard)
        changepoint = float((run_probability[:live] * predictive * hazard).sum())

        updated = np.zeros(max_run + 1)
        updated[0] = changepoint
        tail = min(live, max_run)
        updated[1:tail + 1] = growth[:tail]
        total = updated.sum()
        if total <= 0:
            updated[:] = 0.0
            updated[0] = 1.0
            total = 1.0
        updated /= total
        change[step] = updated[0]

        new_alpha = np.empty(max_run + 1)
        new_beta = np.empty(max_run + 1)
        new_alpha[0], new_beta[0] = alpha0, beta0
        new_alpha[1:tail + 1] = alpha[:tail] + value
        new_beta[1:tail + 1] = beta[:tail] + 1.0
        if tail + 1 <= max_run:
            new_alpha[tail + 1:] = alpha0
            new_beta[tail + 1:] = beta0
        run_probability, alpha, beta = updated, new_alpha, new_beta
    return change


# --------------------------------------------------------------------------- #
# Spend-latency survival baseline
# --------------------------------------------------------------------------- #
class LatencySurvival:
    """Kaplan-Meier spend-latency baseline, stratified by value decile.

    Spend latency is a survival problem, not a threshold: an output that is still
    unspent at the end of the snapshot is *right-censored*, not slow — which is
    exactly what the engine's own `window_boundary_incomplete` coverage flag
    already admits.  Scoring a spend by its survival percentile within its own
    value stratum turns "under one hour" into "faster than 99.4% of comparable
    outputs", which is both graded and reviewable.
    """

    def __init__(self, strata: int = 10) -> None:
        self._strata = strata
        self._edges: np.ndarray | None = None
        self._curves: list[tuple[np.ndarray, np.ndarray]] = []

    def fit(self, values: np.ndarray, durations: np.ndarray, observed: np.ndarray) -> LatencySurvival:
        values = np.asarray(values, dtype=np.float64)
        durations = np.asarray(durations, dtype=np.float64)
        observed = np.asarray(observed, dtype=bool)
        quantiles = np.linspace(0.0, 1.0, self._strata + 1)[1:-1]
        self._edges = np.quantile(np.log1p(np.maximum(values, 0)), quantiles) if values.size else np.array([])
        stratum = self._stratum(values)
        for slot in range(self._strata):
            mask = stratum == slot
            if not mask.any():
                self._curves.append((np.array([0.0]), np.array([1.0])))
                continue
            self._curves.append(_kaplan_meier(durations[mask], observed[mask]))
        return self

    def _stratum(self, values: np.ndarray) -> np.ndarray:
        if self._edges is None or self._edges.size == 0:
            return np.zeros(values.shape[0], dtype=np.int64)
        return np.searchsorted(self._edges, np.log1p(np.maximum(values, 0)), side="right")

    def surprisal(self, values: np.ndarray, durations: np.ndarray) -> np.ndarray:
        """-log S(t) is meaningless for *fast* spends, so use -log(1 - S(t)):
        the smaller the probability of being spent this quickly, the higher."""
        values = np.asarray(values, dtype=np.float64)
        durations = np.asarray(durations, dtype=np.float64)
        stratum = self._stratum(values)
        out = np.zeros(durations.shape[0], dtype=np.float64)
        for slot, (times, survival) in enumerate(self._curves):
            mask = stratum == slot
            if not mask.any():
                continue
            position = np.searchsorted(times, durations[mask], side="right") - 1
            position = np.clip(position, 0, survival.shape[0] - 1)
            cumulative = np.clip(1.0 - survival[position], EPSILON, 1.0)
            out[mask] = -np.log(cumulative)
        return out


def _kaplan_meier(durations: np.ndarray, observed: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(durations, kind="stable")
    durations, observed = durations[order], observed[order]
    unique = np.unique(durations)
    at_risk = durations.shape[0]
    survival = np.empty(unique.shape[0], dtype=np.float64)
    running = 1.0
    cursor = 0
    for slot, moment in enumerate(unique):
        block = 0
        events = 0
        while cursor + block < durations.shape[0] and durations[cursor + block] == moment:
            events += int(observed[cursor + block])
            block += 1
        if at_risk > 0 and events > 0:
            running *= 1.0 - events / at_risk
        survival[slot] = running
        at_risk -= block
        cursor += block
    return unique, survival
