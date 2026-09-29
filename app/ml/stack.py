"""Top-level orchestration: facts -> grains -> layers -> fusion -> report.

One entry point, `run_stack`, so the CLI, the tests and any future API call all
exercise the same path.  Thread and memory bounds are read from the environment
before numpy is used, matching the Phase 5B protocol's posture: a safe bound, not
"use every core".
"""

from __future__ import annotations

import os
import platform
import resource
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.ml import grains, layers
from app.ml.facts import SPLIT_NAMES, Facts, assign_splits, load_facts, split_boundaries

SEED = 42


def configure_threads() -> int:
    """Bound BLAS/OpenMP threads before numpy touches them."""
    threads = int(os.environ.get("TRACEX_ML_THREADS", str(min(4, os.cpu_count() or 1))))
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(variable, str(threads))
    return threads


def peak_rss_mb() -> float:
    """Peak resident set size in MB.

    `ru_maxrss` is bytes on macOS and kilobytes on Linux, so the unit has to come
    from the platform rather than from the magnitude — guessing by magnitude
    reports a 700 MB run as 716,800 MB on a Mac.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 ** 2 if platform.system() == "Darwin" else 1024.0
    return usage / divisor


@dataclass
class StackConfig:
    dataset: Path
    layers: tuple[str, ...] = ("A", "B", "C", "D", "E")
    stratified: bool = True
    with_global_variant: bool = True
    with_hbos: bool = False
    with_bocpd: bool = True
    with_supervised: bool = False
    budget: float = 0.01
    bucket_seconds: int = 900
    shrinkage: float = 4.0
    clusters: int = 8
    threads: int = field(default_factory=configure_threads)


@dataclass
class StackResult:
    facts: Facts
    splits: np.ndarray
    transaction_table: grains.Table
    graph_table: grains.Table | None
    layer_scores: dict[str, np.ndarray]
    layer_objects: dict[str, layers.LayerScore]
    baseline: np.ndarray
    timings: dict[str, float]
    peak_rss_mb: float
    notes: dict[str, object]


def run_stack(
    config: StackConfig,
    *,
    truth_path: Path | None = None,
    supervised_labels: np.ndarray | None = None,
) -> StackResult:
    """Build every grain and run every requested layer.

    `truth_path` is used only to derive the three time-ordered split boundaries.
    No label is read here; `app.ml.evaluate.load_labels` does that separately.

    `supervised_labels` is the single exception: layer S is a supervised
    comparator and needs a target.  It is opt-in, it fits on reference rows only,
    and the caller is responsible for knowing where those labels came from.
    """
    import json

    timings: dict[str, float] = {}
    notes: dict[str, object] = {"threads": config.threads, "seed": SEED}

    start = time.perf_counter()
    facts = load_facts(config.dataset, with_network_context="E" in config.layers)
    timings["load_facts"] = time.perf_counter() - start

    truth_path = truth_path or (config.dataset / "evaluation_truth.json")
    truth = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    boundaries = split_boundaries(config.dataset, truth)
    splits = assign_splits(facts.tx_time, boundaries)
    train_mask = splits == 0
    notes["split_counts"] = {name: int((splits == index).sum()) for index, name in enumerate(SPLIT_NAMES)}

    start = time.perf_counter()
    transaction_table = grains.build_transaction_table(facts)
    timings["grain_a"] = time.perf_counter() - start

    layer_objects: dict[str, layers.LayerScore] = {}

    baseline = layers.rule_baseline(facts, transaction_table)
    layer_objects["rule_baseline"] = baseline
    family_flags = layers.motif_family_flags(facts, transaction_table)
    notes["family_flag_counts"] = {name: int(flag.sum()) for name, flag in family_flags.items()}

    if "A" in config.layers:
        start = time.perf_counter()
        layer_objects["A_structure"] = layers.layer_a_structure(
            transaction_table, train_mask,
            n_clusters=config.clusters, stratified=config.stratified, n_jobs=config.threads,
        )
        timings["layer_a"] = time.perf_counter() - start
        if config.stratified and config.with_global_variant:
            # Also score grain A globally.  Stratification asks "is this odd for
            # something of its kind"; that is the right question when the target is
            # unknown, and the wrong one when the target *is* a shape family, since
            # the family's members are then the majority of their own cluster.
            # Running both makes the trade-off a measurement instead of a guess.
            start = time.perf_counter()
            globals_ = layers.layer_a_structure(
                transaction_table, train_mask,
                n_clusters=config.clusters, stratified=False, n_jobs=config.threads,
            )
            globals_.name = "A_global"
            layer_objects["A_global"] = globals_
            timings["layer_a_global"] = time.perf_counter() - start
        if config.with_hbos:
            start = time.perf_counter()
            layer_objects["A_hbos"] = layers.layer_a_hbos(transaction_table, train_mask)
            timings["layer_a_hbos"] = time.perf_counter() - start

    if "B" in config.layers:
        start = time.perf_counter()
        latency = grains.build_latency_table(facts)
        layer_objects["B_latency"] = layers.layer_b_latency(latency, facts, train_mask)
        timings["layer_b"] = time.perf_counter() - start

    if "C" in config.layers:
        start = time.perf_counter()
        motif_flag = np.maximum(family_flags["coinjoin_like"], family_flags["peel_step"])
        history = grains.build_history_table(facts, motif_flag=motif_flag)
        layer_objects["C_history"] = layers.layer_c_history(
            history, facts, train_mask, shrinkage=config.shrinkage
        )
        timings["layer_c"] = time.perf_counter() - start

    if "D" in config.layers:
        start = time.perf_counter()
        layer_objects["D_burst"] = layers.layer_d_burst(
            facts, family_flags, bucket_seconds=config.bucket_seconds, with_bocpd=config.with_bocpd
        )
        timings["layer_d"] = time.perf_counter() - start

    graph_table = None
    if "E" in config.layers:
        start = time.perf_counter()
        graph_table = grains.build_graph_table(facts)
        layer_objects["E_graph"] = layers.layer_e_graph(graph_table, train_mask, n_jobs=config.threads)
        timings["layer_e"] = time.perf_counter() - start

    if config.with_supervised and supervised_labels is not None:
        start = time.perf_counter()
        tables = [transaction_table] + ([graph_table] if graph_table is not None else [])
        extra = {
            name: item.score for name, item in layer_objects.items()
            if name in {"B_latency", "C_history", "D_burst"}
        }
        layer_objects["S_supervised"] = layers.layer_s_supervised(
            tables, extra, train_mask, supervised_labels, n_jobs=config.threads
        )
        timings["layer_s"] = time.perf_counter() - start

    layer_scores = {
        name: item.score for name, item in layer_objects.items() if name != "rule_baseline"
    }
    for name, item in layer_objects.items():
        if item.notes:
            notes[f"{name}_notes"] = item.notes

    return StackResult(
        facts=facts,
        splits=splits,
        transaction_table=transaction_table,
        graph_table=graph_table,
        layer_scores=layer_scores,
        layer_objects=layer_objects,
        baseline=baseline.score,
        timings=timings,
        peak_rss_mb=peak_rss_mb(),
        notes=notes,
    )


def explain(result: StackResult, transaction: int, *, top: int = 5) -> dict[str, object]:
    """Why is this transaction ranked where it is?

    Returns the per-layer score plus the strongest ECOD feature contributions,
    which is the material a ranked finding needs to be reviewable rather than
    merely ordered.
    """
    payload: dict[str, object] = {
        "txid": result.facts.txids[transaction],
        "block_time": int(result.facts.tx_time[transaction]),
        "split": SPLIT_NAMES[int(result.splits[transaction])],
        "layers": {name: float(score[transaction]) for name, score in result.layer_scores.items()},
        "rule_baseline": float(result.baseline[transaction]),
    }
    drivers: dict[str, list[tuple[str, float]]] = {}
    for name, item in result.layer_objects.items():
        if item.attribution is None:
            continue
        contributions = item.attribution[transaction]
        order = np.argsort(-contributions)[:top]
        drivers[name] = [(item.attribution_columns[index], float(contributions[index])) for index in order]
    payload["drivers"] = drivers
    structure = result.layer_objects.get("A_structure")
    if structure is not None and "shape_family" in structure.detail:
        payload["shape_family"] = int(structure.detail["shape_family"][transaction])
    return payload
