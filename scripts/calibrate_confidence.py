"""Produce a separately versioned diagnostic confidence-calibration artifact.

Run on a database produced by importing the labelled synthetic fixture
(`datasets/phase5a_100k/ingestion_rows.ndjson`). Labels are read here and only
here -- they never reach a detector or a model fit; this script only learns a
monotone map from each rule's own raw score to the observed rate at which
findings with that score are true laundering-pattern leads.

    uv run --extra ml python scripts/calibrate_confidence.py \
        --database var/prof/eq_bounded_512.db --dataset datasets/phase5a_100k \
        --calibration-id confidence-research-NEW --output experiments/runs/NEW.json

A finding is labelled a true lead when at least half of the transactions it
cites are generator-labelled positives (peel_step or coinjoin_like). Splits are
by time, exactly as the anomaly-stack protocol defines them: the map is fitted
on `train_reference` and its reliability is measured on `validation`; the
final-period findings are excluded. Candidate maps are refitted on train +
validation, but are NOT installed or promoted by this command. Weak reliability
must be reported, not hidden by replacing the active registry.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.confidence import TARGET, feature_contract
from app.ml.evaluate import POSITIVE_FAMILIES
from app.ml.facts import split_boundaries

REPO = Path(__file__).resolve().parents[1]
MIN_FINDINGS = 30


def _txids_by_row(dataset: Path) -> list[str | None]:
    rows: list[str | None] = []
    with (dataset / "ingestion_rows.ndjson").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line).get("txid"))
    return rows


def _ece(probability: np.ndarray, label: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for low, high in itertools.pairwise(edges):
        mask = (probability >= low) & ((probability < high) if high < 1 else (probability <= high))
        if mask.any():
            total += mask.mean() * abs(probability[mask].mean() - label[mask].mean())
    return float(total)


def _reliability_bins(probability, label):
    return [{"lower": index / 10, "upper": (index + 1) / 10, "samples": int(mask.sum()),
             "mean_prediction": float(probability[mask].mean()) if mask.any() else None,
             "positive_fraction": float(label[mask].mean()) if mask.any() else None}
            for index in range(10) for mask in [(probability >= index / 10) &
                (probability <= 1 if index == 9 else probability < (index + 1) / 10)]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("datasets/phase5a_100k"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calibration-id", help="New diagnostic identity; never reuse a promoted registry ID")
    args = parser.parse_args()
    identity = args.calibration_id or "confidence-research-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity):
        parser.error("calibration identity must be a safe version name, not a path")
    if identity == "confidence-v1":
        parser.error("confidence-v1 is the frozen registry; choose a new diagnostic identity")
    if args.output is None:
        args.output = REPO / "experiments" / "runs" / f"{identity}.json"
    if args.output.resolve().is_relative_to(REPO / "app" / "engine" / "calibration"):
        parser.error("write a diagnostic artifact outside the active registry; promotion requires a separate reviewed decision")
    if args.output.exists():
        raise FileExistsError(args.output)

    truth = json.loads((args.dataset / "evaluation_truth.json").read_text(encoding="utf-8"))
    if not truth.get("evaluation_only"):
        raise SystemExit("evaluation truth is not marked evaluation_only; refusing to use it")
    positive = {txid for txid, record in truth["labelled_transactions"].items()
                if record["family"] in POSITIVE_FAMILIES}
    validation_start, holdout_start = split_boundaries(args.dataset, truth)
    row_txids = _txids_by_row(args.dataset)

    tx_times = {}
    with (args.dataset / "transactions.ndjson").open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            tx_times[record["txid"]] = datetime.fromisoformat(record["block_time"]).timestamp()
    connection = sqlite3.connect(f"file:{args.database.resolve(strict=True)}?mode=ro", uri=True)
    rows = connection.execute(
        "SELECT rule_id, rule_version, raw_score, window_start, entity_ref, source_refs FROM findings"
    ).fetchall()
    connection.close()
    per_rule: dict[tuple[str, str], dict] = {}
    skipped_holdout = 0
    for rule_id, rule_version, raw_score, window_start, entity_ref, source_refs in rows:
        start = datetime.fromisoformat(str(window_start))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        moment = start.timestamp()
        txids: set[str] = set()
        if entity_ref.startswith("tx:"):
            txids.add(entity_ref[3:])
        for ref in json.loads(source_refs or "[]"):
            locator = str(ref.get("locator") or "")
            if ref.get("locator_type") == "json_pointer" and locator.startswith("/") and locator[1:].isdigit():
                index = int(locator[1:])
                if 0 <= index < len(row_txids) and row_txids[index]:
                    txids.add(row_txids[index])
        if not txids:
            continue
        # A window starting in training may cite later transactions. Assign it
        # by the latest cited time and exclude any final-period evidence.
        moment = max([moment, *[tx_times[txid] for txid in txids if txid in tx_times]])
        if moment >= holdout_start:
            skipped_holdout += 1
            continue
        split = "train" if moment < validation_start else "validation"
        share = sum(1 for txid in txids if txid in positive) / len(txids)
        bucket = per_rule.setdefault((rule_id, rule_version), {"score": [], "label": [], "split": [], "version": rule_version})
        bucket["score"].append(float(raw_score))
        bucket["label"].append(1.0 if share >= 0.5 else 0.0)
        bucket["split"].append(split)

    rules: dict[str, dict] = {}
    for (rule_id, rule_version), bucket in sorted(per_rule.items()):
        score = np.asarray(bucket["score"])
        label = np.asarray(bucket["label"])
        train = np.asarray([split == "train" for split in bucket["split"]])
        validation = ~train
        entry: dict = {"rule_id": rule_id, "rule_version": rule_version,
                       "feature_contract": feature_contract(rule_version), "target_definition": TARGET,
                       "findings": int(score.size),
                       "positives": int(label.sum()), "base_rate": float(label.mean())}
        if score.size < MIN_FINDINGS or label.sum() == 0 or label.sum() == score.size:
            # Too few, or no variation: a smoothed constant rate is the honest map.
            rate = (label.sum() + 1) / (score.size + 2)
            entry.update({"method": "laplace_constant", "x": [], "y": [], "constant": float(rate)})
            if validation.any() and train.any():
                training_rate = (label[train].sum() + 1) / (train.sum() + 2)
                predicted = np.full(validation.sum(), training_rate)
                entry["validation"] = {
                    "findings": int(validation.sum()), "positives": int(label[validation].sum()),
                    "brier": float(brier_score_loss(label[validation], predicted)),
                    "ece": _ece(predicted, label[validation]),
                    "bins": _reliability_bins(predicted, label[validation]),
                }
        else:
            if train.sum() >= MIN_FINDINGS and validation.sum() >= 10 and 0 < label[train].sum() < train.sum():
                model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(score[train], label[train])
                predicted = model.predict(score[validation])
                held = label[validation]
                entry["validation"] = {
                    "findings": int(validation.sum()), "positives": int(held.sum()),
                    "brier": float(brier_score_loss(held, predicted)),
                    "brier_base_rate": float(brier_score_loss(held, np.full(held.size, label[train].mean()))),
                    "ece": _ece(predicted, held),
                    "bins": _reliability_bins(predicted, held),
                    "auc": float(roc_auc_score(held, predicted)) if 0 < held.sum() < held.size else None,
                }
            final = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(score, label)
            # Shrink the extremes a little toward the base rate so no finding is
            # ever reported as certain (0 or 1) from a finite labelled sample.
            n = score.size
            y = (final.y_thresholds_ * n + label.mean()) / (n + 1)
            entry.update({"method": "isotonic", "x": [float(v) for v in final.X_thresholds_],
                          "y": [float(v) for v in y]})
        rules["|".join((rule_id, rule_version, feature_contract(rule_version), TARGET))] = entry

    with (args.dataset / "ingestion_rows.ndjson").open("rb") as handle:
        ingestion_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    payload = {
        "calibration_id": identity,
        "diagnostic_only": True,
        "promoted": False,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "definition": ("Synthetic benchmark majority-pattern frequency conditional on rule and raw score: "
                       "at least half of cited transactions are generator-labelled peel_step or coinjoin_like. "
                       "Not a probability of real-world criminality."),
        "fit": "isotonic regression per rule; reliability measured on the time-ordered validation split; "
               "candidate map refitted on train + validation; final-period findings excluded; not installed",
        "fixture": {
            "dataset": str(args.dataset),
            "generator_version": truth.get("generator_version"),
            "truth_sha256": hashlib.sha256((args.dataset / "evaluation_truth.json").read_bytes()).hexdigest(),
            "ingestion_sha256": [ingestion_hash],
        },
        "holdout_findings_skipped": skipped_holdout,
        "rules": rules,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    for rule_id, entry in rules.items():
        print(rule_id, entry["method"], entry["findings"], entry["positives"],
              {key: round(value, 4) if isinstance(value, float) else value
               for key, value in (entry.get("validation") or {}).items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
