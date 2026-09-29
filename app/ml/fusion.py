"""Layer F — turn several layer scores into one ranked, budgeted review queue.

Two steps, both predeclared rather than tuned after seeing results:

1. Each layer's score becomes a p-value through an empirical CDF fitted on the
   `train_reference` rows only, then the p-values are combined by Stouffer's
   method with fixed weights.  Combining p-values rather than raw scores is what
   lets layers with completely different scales (a path length, a log tail
   probability, a Poisson surprisal) be added at all.

2. The cut is a conformal quantile of the reference distribution, so the review
   budget is a number chosen up front.  The measured alternative on the old
   fixture was a rule that flagged 21.2% of all windows, which is not a queue
   anyone works through.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

EPSILON = 1e-9


@dataclass(slots=True)
class FusedScore:
    name: str
    score: np.ndarray
    p_values: dict[str, np.ndarray]
    threshold: float
    flagged: np.ndarray
    weights: dict[str, float]


def _ecdf_p_values(score: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Right-tail p-value of every score against the reference distribution.

    Fitted on reference rows only, so a validation or holdout row can never
    influence the calibration it is judged by.
    """
    ordered = np.sort(reference)
    n = ordered.shape[0]
    above = n - np.searchsorted(ordered, score, side="left")
    return np.clip((above + 1.0) / (n + 1.0), EPSILON, 1.0 - EPSILON)


def stouffer_fuse(
    layer_scores: dict[str, np.ndarray],
    reference_mask: np.ndarray,
    *,
    weights: dict[str, float] | None = None,
    budget: float = 0.01,
) -> FusedScore:
    """Weighted Stouffer combination of per-layer ECDF p-values.

    `budget` is the fraction of rows to flag: the threshold is the corresponding
    quantile of the *reference* distribution, so the flag rate on unseen data is
    an honest estimate rather than a re-tuned cut.
    """
    if not layer_scores:
        raise ValueError("fusion needs at least one layer score")
    names = sorted(layer_scores)
    weights = weights or {name: 1.0 for name in names}
    missing = [name for name in names if name not in weights]
    if missing:
        raise ValueError(f"no fusion weight declared for {missing}")

    p_values: dict[str, np.ndarray] = {}
    numerator = np.zeros(next(iter(layer_scores.values())).shape[0], dtype=np.float64)
    denominator = 0.0
    for name in names:
        values = np.asarray(layer_scores[name], dtype=np.float64)
        p = _ecdf_p_values(values, values[reference_mask])
        p_values[name] = p
        weight = float(weights[name])
        # -norm.isf(p) is the one-sided z-score; larger p (less surprising) pulls
        # the combined statistic down, which is the behaviour we want.
        numerator += weight * norm.isf(p)
        denominator += weight ** 2
    combined = numerator / np.sqrt(max(denominator, EPSILON))

    threshold = float(np.quantile(combined[reference_mask], 1.0 - budget))
    return FusedScore(
        name="F_fusion",
        score=combined,
        p_values=p_values,
        threshold=threshold,
        flagged=combined >= threshold,
        weights={name: float(weights[name]) for name in names},
    )


def rank_average_fuse(layer_scores: dict[str, np.ndarray], reference_mask: np.ndarray, *, budget: float = 0.01) -> FusedScore:
    """Simpler alternative: average of rank-normalised scores.

    Kept because it is the obvious baseline for fusion itself — if Stouffer does
    not beat a plain rank average, the extra machinery is not earning its place.
    """
    names = sorted(layer_scores)
    total = np.zeros(next(iter(layer_scores.values())).shape[0], dtype=np.float64)
    for name in names:
        values = np.asarray(layer_scores[name], dtype=np.float64)
        order = np.argsort(values, kind="stable")
        ranks = np.empty(values.shape[0], dtype=np.float64)
        ranks[order] = np.arange(values.shape[0], dtype=np.float64)
        total += ranks / max(values.shape[0] - 1, 1)
    combined = total / len(names)
    threshold = float(np.quantile(combined[reference_mask], 1.0 - budget))
    return FusedScore(
        name="F_rank_average",
        score=combined,
        p_values={},
        threshold=threshold,
        flagged=combined >= threshold,
        weights={name: 1.0 for name in names},
    )
