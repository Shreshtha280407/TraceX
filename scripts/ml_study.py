#!/usr/bin/env python3
"""Can preprocessing, detector settings or fusion improve the deployed anomaly stack?

    uv run --extra ml python scripts/ml_study.py --dataset datasets/phase5a_100k --dataset datasets/judge_a ...

For every dataset given, fits on its `train_reference` split exactly as the
offline harness does and reports average precision (AP) on development
`validation` only, for three tasks (motif,
surge, discrimination). Every variant changes ONE thing relative to the frozen
release (A_global + D_burst, Stouffer, equal weights):

* layer-A preprocessing: StandardScaler (frozen) / QuantileTransformer(normal) /
  signed log1p + StandardScaler;
* layer-A detectors: Isolation Forest (v2) / historical IF+ECOD / + HBOS / ECOD only;
* Isolation Forest capacity: 100 x 256 (frozen) / 300 x 512;
* fusion weights A:D: 1:1 (frozen) / 1.5:1 / 1:1.5.

A variant is worth adopting only if it beats the frozen release on the
validation split of *every* dataset (no dataset-specific tuning) and does not
lose on holdout or on discrimination. Labels are read only to score.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import QuantileTransformer, StandardScaler

from app.ml import evaluate as ev
from app.ml import grains, layers
from app.ml.detectors import ECOD, HBOS
from app.ml.facts import SPLIT_NAMES, assign_splits, load_facts, split_boundaries
from app.ml.fusion import stouffer_fuse

SEED = 42
TASKS = ("motif", "surge", "discrimination")


def _scaled(matrix: np.ndarray, train: np.ndarray, method: str) -> np.ndarray:
    if method == "log1p":
        matrix = np.sign(matrix) * np.log1p(np.abs(matrix))
        method = "standard"
    if method == "standard":
        return StandardScaler().fit(matrix[train]).transform(matrix)
    if method == "quantile":
        transformer = QuantileTransformer(output_distribution="normal", n_quantiles=1000, subsample=200_000,
                                          random_state=SEED)
        return transformer.fit(matrix[train]).transform(matrix)
    raise ValueError(method)


def layer_a(matrix: np.ndarray, train: np.ndarray, *, scaling: str = "standard", detectors: str = "if",
            trees: int = 100, samples: int = 256) -> np.ndarray:
    scaled = _scaled(matrix.astype(np.float64), train, scaling)
    reference = scaled[train]
    scores = []
    if "if" in detectors.split("+"):
        forest = IsolationForest(n_estimators=trees, max_samples=min(samples, reference.shape[0]),
                                 random_state=SEED, n_jobs=-1).fit(reference)
        scores.append(layers._rank_normalise(-forest.score_samples(scaled), train))
    if "ecod" in detectors.split("+"):
        ecod = ECOD().fit(reference)
        scores.append(layers._rank_normalise(ecod.contributions(scaled).sum(axis=1), train))
    if "hbos" in detectors.split("+"):
        scores.append(layers._rank_normalise(HBOS().fit(reference).score(scaled), train))
    return np.mean(scores, axis=0)


VARIANTS = {
    "frozen (A_global+D_burst)": {},
    "historical v1 IF+ECOD": {"detectors": "if+ecod"},
    "A: quantile-normal scaling": {"scaling": "quantile"},
    "A: signed log1p + standard": {"scaling": "log1p"},
    "A: IF+ECOD+HBOS": {"detectors": "if+ecod+hbos"},
    "A: IF only": {"detectors": "if"},
    "A: ECOD only": {"detectors": "ecod"},
    "A: IF 300 trees x 512": {"trees": 300, "samples": 512},
    "fusion A:D = 1.5:1": {"weights": (1.5, 1.0)},
    "fusion A:D = 1:1.5": {"weights": (1.0, 1.5)},
    "IF only + A:D 1:1.25": {"detectors": "if", "weights": (1.0, 1.25)},
    "IF only + A:D 1:1.5": {"detectors": "if", "weights": (1.0, 1.5)},
}


def study(dataset: Path) -> dict:
    started = time.time()
    facts = load_facts(dataset, with_network_context=False)
    truth = json.loads((dataset / "evaluation_truth.json").read_text(encoding="utf-8"))
    splits = assign_splits(facts.tx_time, split_boundaries(dataset, truth))
    labels = ev.load_labels(facts, dataset / "evaluation_truth.json")
    train = splits == 0
    table = grains.build_transaction_table(facts)
    flags = layers.motif_family_flags(facts, table)
    burst = layers.layer_d_burst(facts, flags, reference=train).score
    cache: dict[tuple, np.ndarray] = {}
    results: dict[str, dict] = {}
    report_splits = ["validation"]
    for name, variant in VARIANTS.items():
        key = tuple(sorted((k, v) for k, v in variant.items() if k != "weights"))
        if key not in cache:
            cache[key] = layer_a(table.matrix, train, **dict(key))
        weights = variant.get("weights", (1.0, 1.0))
        fused = stouffer_fuse({"A_global": cache[key], "D_burst": burst}, train,
                              weights={"A_global": weights[0], "D_burst": weights[1]}).score
        results[name] = {}
        for split in report_splits:
            for task in TASKS:
                _, population = ev.task_targets(labels, task)
                threshold = float(np.quantile(fused[train & population], 0.99))
                metric = ev.evaluate(name, fused, labels, splits, split=split, task=task, budget=0.01,
                                     reference_threshold=threshold)
                results[name][f"{split}:{task}"] = round(metric.average_precision, 4)
    return {"dataset": str(dataset), "transactions": facts.transaction_count, "seconds": round(time.time() - started, 1),
            "split_counts": {name: int((splits == index).sum()) for index, name in enumerate(SPLIT_NAMES)},
            "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, default=REPO / "experiments" / "runs" / "ml_study.json")
    parser.add_argument("--variants", default=None, help="comma-separated subset of variant names (frozen always runs)")
    parser.add_argument("--controlled", action="store_true", help="run pre-registered HGB/XGBoost/LightGBM/hybrid grid")
    parser.add_argument("--final-candidate", help="evaluate one frozen controlled candidate once on newly reserved holdouts")
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--release-protocol", type=Path)
    parser.add_argument("--frozen-selection", type=Path)
    args = parser.parse_args()
    if args.controlled:
        from scripts.ml_controlled_study import run

        run(args.dataset, args.output, final_candidate=args.final_candidate, max_iter=args.max_iterations,
            release_protocol=args.release_protocol, frozen_selection=args.frozen_selection)
        return 0
    if args.variants:
        keep = {name.strip() for name in args.variants.split(",")} | {"frozen (A_global+D_burst)"}
        for name in list(VARIANTS):
            if name not in keep:
                del VARIANTS[name]
    reports = [study(path) for path in args.dataset]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(reports, handle, indent=2)
    for report in reports:
        print(f"\n== {report['dataset']}  ({report['transactions']:,} txs, {report['seconds']} s, splits {report['split_counts']})")
        columns = list(next(iter(report["results"].values())))
        print(f"{'variant':<30}" + "".join(f"{column:>26}" for column in columns))
        base = report["results"]["frozen (A_global+D_burst)"]
        for name, values in report["results"].items():
            cells = "".join(
                f"{values[column]:>18.4f} ({values[column] - base[column]:+.3f})" if name != "frozen (A_global+D_burst)"
                else f"{values[column]:>26.4f}" for column in columns
            )
            print(f"{name:<30}{cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
