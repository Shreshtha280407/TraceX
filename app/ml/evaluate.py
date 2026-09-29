"""Metrics and the ablation harness.

The point of this module is not to declare a winner in advance.  It runs every
layer alone, every useful combination, and the deterministic rule baseline over
the same transactions, the same split and the same review budget, and prints the
numbers side by side so the best combination is chosen from evidence.

Labels are read here and only here.  They never enter a feature table and never
reach a `fit()`.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from app.ml.facts import SPLIT_NAMES, Facts
from app.ml.fusion import rank_average_fuse, stouffer_fuse

POSITIVE_FAMILIES = ("coinjoin_like", "peel_step")
# Families that sit deliberately close to a motif.  A candidate that cannot keep
# these out of the queue has learned the neighbourhood, not the shape.
NEAR_MISS_FAMILIES = (
    "nearmiss_two_equal", "nearmiss_tolerance", "nearmiss_two_inputs", "nearmiss_batch",
    # Satisfies the deterministic rule's predicate exactly while being an ordinary
    # recurring payment. This is the family that decides whether a model is worth
    # anything over a threshold.
    "nearmiss_rule_positive",
)


@dataclass
class Labels:
    positive: np.ndarray       # bool, per transaction
    near_miss: np.ndarray      # bool, per transaction
    family: np.ndarray         # int8 index into `families`, -1 when unlabelled
    families: tuple[str, ...]
    in_surge: np.ndarray       # bool, member of a labelled burst episode


def load_labels(facts: Facts, truth_path: Path) -> Labels:
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest"):
        raise ValueError("evaluation truth is not marked evaluation_only/do_not_ingest — refusing to use it")
    count = facts.transaction_count
    families: list[str] = []
    family_index: dict[str, int] = {}
    family = np.full(count, -1, dtype=np.int8)
    in_surge = np.zeros(count, dtype=bool)
    for txid, record in truth["labelled_transactions"].items():
        slot = facts.tx_index.get(txid, -1)
        if slot < 0:
            continue
        name = record["family"]
        if name not in family_index:
            family_index[name] = len(families)
            families.append(name)
        family[slot] = family_index[name]
        in_surge[slot] = bool(record.get("in_surge"))
    positive = np.zeros(count, dtype=bool)
    near_miss = np.zeros(count, dtype=bool)
    for name in POSITIVE_FAMILIES:
        if name in family_index:
            positive |= family == family_index[name]
    for name in NEAR_MISS_FAMILIES:
        if name in family_index:
            near_miss |= family == family_index[name]
    return Labels(positive, near_miss, family, tuple(families), in_surge)


@dataclass
class Metrics:
    candidate: str
    split: str
    task: str
    rows: int
    positives: int
    precision_at_20: float
    precision_at_50: float
    precision_at_100: float
    precision_at_500: float
    recall_at_budget: float
    precision_at_budget: float
    near_miss_rate_at_budget: float
    average_precision: float
    roc_auc: float
    flagged_at_budget: int
    notes: dict = field(default_factory=dict)


def _precision_at_k(score: np.ndarray, positive: np.ndarray, k: int) -> float:
    if score.shape[0] == 0:
        return float("nan")
    k = min(k, score.shape[0])
    top = np.argpartition(-score, k - 1)[:k] if k < score.shape[0] else np.arange(score.shape[0])
    return float(positive[top].mean())


def _average_precision(score: np.ndarray, positive: np.ndarray) -> float:
    """Average precision with ties resolved by expectation, not by array order.

    This matters for the comparison to be fair at all: the deterministic rule
    assigns only three distinct scores, so with a stable sort its tied block
    would be ordered by transaction index — an arbitrary ordering that inflates
    or deflates its precision for no real reason.  Averaging over tie orderings
    gives every candidate the score it actually earns.
    """
    if not positive.any():
        return float("nan")
    order = np.argsort(-score, kind="stable")
    ordered_score = score[order]
    hits = positive[order].astype(np.float64)
    cumulative = np.cumsum(hits)
    ranks = np.arange(1, hits.shape[0] + 1, dtype=np.float64)

    # Replace each tie block's running counts with its block-end values, then
    # credit each positive with the block's mean precision.
    boundaries = np.flatnonzero(np.append(np.diff(ordered_score) != 0, True))
    block_end = np.repeat(boundaries, np.diff(np.append(-1, boundaries)))
    precision = cumulative[block_end] / ranks[block_end]
    return float((precision * hits).sum() / hits.sum())


def _roc_auc(score: np.ndarray, positive: np.ndarray) -> float:
    if not positive.any() or positive.all():
        return float("nan")
    order = np.argsort(score, kind="stable")
    ranks = np.empty(score.shape[0], dtype=np.float64)
    ranks[order] = np.arange(1, score.shape[0] + 1)
    n_pos = float(positive.sum())
    n_neg = float((~positive).sum())
    return float((ranks[positive].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


#: The three questions the problem statement actually asks, each with its own
#: positive set and its own population.  Reporting one number over one of them
#: would hide that the layers are good at different things.
TASKS: dict[str, str] = {
    "motif": "rank CoinJoin-like and peeling transactions above everything else",
    "surge": "rank transactions inside a labelled motif surge episode above the rest",
    "discrimination": "rank true motifs above the labelled near-miss negatives only",
}


def task_targets(labels: Labels, task: str) -> tuple[np.ndarray, np.ndarray]:
    """(positive, population) masks for one task.

    `discrimination` restricts the population to motifs plus near-misses, which is
    the only way to measure whether a candidate separates a real equal-output
    structure from something that merely looks like one.
    """
    if task == "motif":
        return labels.positive, np.ones(labels.positive.shape[0], dtype=bool)
    if task == "surge":
        return labels.in_surge, np.ones(labels.positive.shape[0], dtype=bool)
    if task == "discrimination":
        return labels.positive, labels.positive | labels.near_miss
    raise ValueError(f"unknown task {task!r}; expected one of {sorted(TASKS)}")


def evaluate(
    candidate: str,
    score: np.ndarray,
    labels: Labels,
    splits: np.ndarray,
    *,
    split: str = "validation",
    task: str = "motif",
    budget: float = 0.01,
    reference_threshold: float | None = None,
    notes: dict | None = None,
) -> Metrics:
    """Score one candidate on one split at a fixed review budget.

    `reference_threshold` is the cut computed on the reference split; passing it
    keeps the budget honest, because a threshold re-derived on the evaluation
    split would silently re-tune the candidate on the data it is being judged by.
    """
    positive_all, population = task_targets(labels, task)
    mask = (splits == SPLIT_NAMES.index(split)) & population
    subset = score[mask]
    positive = positive_all[mask]
    near_miss = labels.near_miss[mask]
    if reference_threshold is None:
        reference_threshold = float(np.quantile(score[(splits == 0) & population], 1.0 - budget))
    flagged = subset >= reference_threshold

    return Metrics(
        candidate=candidate,
        split=split,
        task=task,
        rows=int(mask.sum()),
        positives=int(positive.sum()),
        precision_at_20=_precision_at_k(subset, positive, 20),
        precision_at_50=_precision_at_k(subset, positive, 50),
        precision_at_100=_precision_at_k(subset, positive, 100),
        precision_at_500=_precision_at_k(subset, positive, 500),
        recall_at_budget=float(positive[flagged].sum() / max(positive.sum(), 1)),
        precision_at_budget=float(positive[flagged].mean()) if flagged.any() else 0.0,
        near_miss_rate_at_budget=float(near_miss[flagged].mean()) if flagged.any() else 0.0,
        average_precision=_average_precision(subset, positive),
        roc_auc=_roc_auc(subset, positive),
        flagged_at_budget=int(flagged.sum()),
        notes=notes or {},
    )


# --------------------------------------------------------------------------- #
# Ablation
# --------------------------------------------------------------------------- #
def layer_combinations(available: list[str], *, max_size: int | None = None) -> list[tuple[str, ...]]:
    """Every non-empty subset of the available layers, smallest first.

    Exhaustive on purpose: the question the user asked is which combination works
    best, and with six layers there are only 63 subsets.  Guessing which ones are
    worth testing would be exactly the shortcut that produces an unjustified
    architecture.
    """
    limit = max_size or len(available)
    result: list[tuple[str, ...]] = []
    for size in range(1, limit + 1):
        result.extend(itertools.combinations(sorted(available), size))
    return result


def candidate_scores(
    layer_scores: dict[str, np.ndarray],
    baseline: np.ndarray,
    reference: np.ndarray,
    *,
    fusion: str = "stouffer",
    budget: float = 0.01,
    max_size: int | None = None,
) -> dict[str, tuple[np.ndarray, list[str]]]:
    """Every candidate's score vector: the rule, each layer, every combination."""
    fuse = stouffer_fuse if fusion == "stouffer" else rank_average_fuse
    out: dict[str, tuple[np.ndarray, list[str]]] = {"rule_baseline": (baseline, [])}
    for combination in layer_combinations(sorted(layer_scores), max_size=max_size):
        subset = {name: layer_scores[name] for name in combination}
        score = (next(iter(subset.values())) if len(combination) == 1
                 else fuse(subset, reference, budget=budget).score)
        out["+".join(combination)] = (score, list(combination))
    return out


def run_ablation(
    layer_scores: dict[str, np.ndarray],
    baseline: np.ndarray,
    labels: Labels,
    splits: np.ndarray,
    *,
    budget: float = 0.01,
    fusion: str = "stouffer",
    max_size: int | None = None,
    split: str = "validation",
    tasks: tuple[str, ...] = ("motif", "surge", "discrimination"),
) -> dict[str, list[Metrics]]:
    """Evaluate the baseline, every layer and every combination, on every task."""
    reference = splits == 0
    candidates = candidate_scores(
        layer_scores, baseline, reference, fusion=fusion, budget=budget, max_size=max_size
    )
    results: dict[str, list[Metrics]] = {}
    for task in tasks:
        _, population = task_targets(labels, task)
        rows: list[Metrics] = []
        for name, (score, combination) in candidates.items():
            threshold = float(np.quantile(score[reference & population], 1.0 - budget))
            rows.append(
                evaluate(name, score, labels, splits, split=split, task=task,
                         budget=budget, reference_threshold=threshold,
                         notes={"kind": "deterministic rule" if not combination else
                                ("single layer" if len(combination) == 1 else fusion),
                                "layers": combination})
            )
        results[task] = rows
    return results


def rank_results(results: list[Metrics], *, key: str = "average_precision") -> list[Metrics]:
    def sort_key(item: Metrics) -> float:
        value = getattr(item, key)
        # A metric is NaN when its task has no usable positives on this split.
        # Sorting those last is deliberate: an undefined metric is never a win.
        return -1.0 if math.isnan(value) else value
    return sorted(results, key=sort_key, reverse=True)


def format_table(results: list[Metrics], *, limit: int = 25) -> str:
    header = (
        f"{'candidate':46s} {'AP':>7} {'AUC':>7} {'P@20':>6} {'P@50':>6} {'P@100':>6} "
        f"{'P@bud':>7} {'R@bud':>7} {'NM@bud':>7} {'flagged':>8}"
    )
    lines = [header, "-" * len(header)]
    for item in results[:limit]:
        lines.append(
            f"{item.candidate:46s} {item.average_precision:7.4f} {item.roc_auc:7.4f} "
            f"{item.precision_at_20:6.3f} {item.precision_at_50:6.3f} {item.precision_at_100:6.3f} "
            f"{item.precision_at_budget:7.4f} {item.recall_at_budget:7.4f} "
            f"{item.near_miss_rate_at_budget:7.4f} {item.flagged_at_budget:8d}"
        )
    return "\n".join(lines)


def to_json(results: list[Metrics]) -> list[dict]:
    return [asdict(item) for item in results]
