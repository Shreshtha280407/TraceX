"""Registered frozen-weight training/comparison and transfer evaluation.

Inputs are canonical fixture directories; truth is opened ONLY in this offline
command, never in product imports. No large study runs without an explicit call.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, confusion_matrix

from app.ml import candidate
from app.ml.facts import load_facts


def population(path, truth_path, exclude_families=(), *, return_families=False):
    research_admission([path])
    facts = load_facts(path)
    truth = json.loads(Path(truth_path).read_text())
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest"):
        raise ValueError("truth must be evaluation_only/do_not_ingest")
    labels = truth.get("labelled_transactions", {})
    selected = sorted((i for i, txid in enumerate(facts.txids) if txid in labels and labels[txid].get("family") not in exclude_families), key=lambda i: facts.txids[i])
    rows = [labels[facts.txids[i]] for i in selected]
    targets = {"motif": np.asarray([bool(r.get("motif_positive", r.get("family") in {"coinjoin_like", "peel_step"})) for r in rows]),
               "surge": np.asarray([bool(r.get("surge_positive", r.get("in_surge"))) for r in rows])}
    targets["discrimination"] = np.asarray([bool(r.get("discrimination_positive", targets["motif"][i] or targets["surge"][i])) for i, r in enumerate(rows)])
    groups = [str(r.get("episode_id") or r.get("scenario_id") or facts.txids[i]) for r, i in zip(rows, selected, strict=True)]
    benign = np.asarray([bool(r.get("benign_control") or str(r.get("family", "")).startswith("benign")
        or r.get("family") in {"nearmiss_rule_positive", "batch_fanout", "consolidation", "nearmiss_batch"}) for r in rows])
    result = candidate.features(facts)[selected], targets, groups, benign
    return (*result, [str(row.get("family", "unknown")) for row in rows]) if return_families else result


def research_admission(paths):
    """Streaming count screen before Python dictionaries/global research arrays."""
    from app.resources import admit_global_allocation
    total = 0
    for directory in paths:
        for name, per_row in (("transactions", 2200), ("outputs", 500), ("inputs", 200)):
            with (Path(directory) / (name + ".ndjson")).open() as stream:
                total += sum(bool(line.strip()) for line in stream) * per_row
    admit_global_allocation("candidate research retained populations/fitting", total * 2 + (512 << 20))


def baseline(dataset, truth_path):
    """Deployed v2 PROCEDURE comparator: label-free refit on this dataset's reference.

    Unlike the candidate this is not frozen-weight transfer; v2 D is retrospective.
    It uses no truth for fitting. The distinction is recorded beside the metrics.
    """
    from app.ml import grains, layers
    from app.ml.findings import A_SCORE_WITH
    from app.ml.fusion import stouffer_fuse
    facts = load_facts(dataset)
    table = grains.build_transaction_table(facts)
    order = np.argsort(facts.tx_time, kind="stable")
    reference = np.zeros(facts.transaction_count, dtype=bool)
    reference[order[:max(1, int(facts.transaction_count * .7))]] = True
    if len(order) < 50:
        raise ValueError("v2 reference needs at least 50 transactions")
    flags = layers.motif_family_flags(facts, table)
    a = layers.layer_a_structure(table, reference, stratified=False, score_with=A_SCORE_WITH)
    d = layers.layer_d_burst(facts, flags, reference=reference)
    score = stouffer_fuse({"A_global": a.score, "D_burst": d.score}, reference).score
    truth = json.loads(Path(truth_path).read_text())["labelled_transactions"]
    selected = sorted((i for i, txid in enumerate(facts.txids) if txid in truth), key=lambda i: facts.txids[i])
    return {task: score[selected] for task in candidate.TASKS}


def metrics(scores, labels, *, budget=100, groups=None, benign=None):
    results = {}
    rng = np.random.default_rng(42)
    for task in candidate.TASKS:
        y, score = np.asarray(labels[task], bool), np.asarray(scores[task])
        if not len(y):
            results[task] = {"status": "no labelled observations"}
            continue
        order = np.argsort(-score, kind="stable")
        predicted = score >= .5
        tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[False, True]).ravel()
        intervals = []
        clusters = np.asarray(groups if groups is not None else np.arange(len(y)), dtype=object)
        unique = np.unique(clusters)
        if len(unique) > 1 and y.any():
            members = [np.flatnonzero(clusters == value) for value in unique]
            for _ in range(100):
                sample = np.concatenate([members[i] for i in rng.integers(0, len(unique), len(unique))])
                if y[sample].any():
                    intervals.append(float(average_precision_score(y[sample], score[sample])))
        results[task] = {"population": len(y), "prevalence": float(y.mean()),
            "ap": float(average_precision_score(y, score)) if y.any() else None,
            "precision": int(tp) / max(1, int(tp + fp)), "recall": int(tp) / max(1, int(tp + fn)),
            "p_at_100": float(y[order[:100]].mean()) if len(y) >= 100 else None,
            "review_budget": budget, "p_at_review_budget": float(y[order[:budget]].mean()) if budget and len(y) >= budget else None,
            "recall_at_review_budget": int(y[order[:budget]].sum()) / max(1, int(y.sum())),
            "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
            "benign_false_positives": int((predicted & benign).sum()) if benign is not None else None,
            "benign_in_review_budget": int(benign[order[:budget]].sum()) if benign is not None else None,
            "brier": float(np.mean((score - y) ** 2)),
            "ap_95": np.quantile(intervals, [.025, .975]).tolist() if intervals else None,
            "uncertainty": "episode/scenario cluster bootstrap, conditional on frozen fitted weights; AP is not accuracy"}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reserve = sub.add_parser("register")
    reserve.add_argument("--protocol", type=Path, required=True)
    reserve.add_argument("--final", action="append", required=True, help="Fresh future dataset path, must not exist yet")
    for command in ("train", "compare"):
        train_parser = sub.add_parser(command)
        train_parser.add_argument("--protocol", type=Path, required=True)
        for split in ("training", "calibration", "validation"):
            train_parser.add_argument("--" + split, type=Path, required=True)
            train_parser.add_argument("--" + split + "-truth", type=Path, required=True)
        train_parser.add_argument("--output", type=Path, required=True)
        train_parser.add_argument("--model", choices=["hist", "xgboost", "lightgbm", "hybrid"], default="hist")
        train_parser.add_argument("--exclude-family", action="append", default=[], help="Pre-registered unseen family: exclude from training/calibration/validation, never from final")
        train_parser.add_argument("--label-domain", default="synthetic-fixture-patterns", help="Documented task-label source domain; not automatic eligibility")
    transfer = sub.add_parser("evaluate")
    transfer.add_argument("--protocol", type=Path, required=True)
    transfer.add_argument("--artifact", type=Path, required=True)
    transfer.add_argument("--manifest-sha256", required=True)
    transfer.add_argument("--dataset", type=Path, required=True)
    transfer.add_argument("--truth", type=Path, required=True)
    transfer.add_argument("--output", type=Path, required=True)
    transfer.add_argument("--review-budget", type=int, default=100)
    promote = sub.add_parser("promote", help="OWNER decision only: never automatically promotes from a label count/AP")
    promote.add_argument("--artifact", type=Path, required=True)
    promote.add_argument("--manifest-sha256", required=True)
    promote.add_argument("--approval", type=Path, required=True)
    promote.add_argument("--output", type=Path, required=True)
    freeze = sub.add_parser("freeze", help="Pin validation-selected weights before opening any fresh final")
    freeze.add_argument("--protocol", type=Path, required=True)
    freeze.add_argument("--artifact", type=Path, required=True)
    freeze.add_argument("--manifest-sha256", required=True)
    freeze.add_argument("--validation-report", type=Path, required=True)
    freeze.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        finals = [str(Path(value).resolve()) for value in args.final]
        if len(set(finals)) != len(finals) or any(Path(value).exists() for value in finals):
            parser.error("reserve fresh, distinct final paths before generation/selection; evaluated holdouts cannot be reused")
        args.protocol.parent.mkdir(parents=True, exist_ok=True)
        with args.protocol.open("x") as stream:
            json.dump({"schema": 1, "reserved_finals": finals, "selection": "validation only; no final labels enter fitting",
                "feature_contract": candidate.CONTRACT, "grid": ["hist", "xgboost", "lightgbm", "hybrid"],
                "generalization": ["fresh seeds/prevalence", "unseen pattern families", "payroll/merchant/exchange/batching/consolidation", "missing/noisy IP/ASN", "incomplete prevouts", "degree/reuse skew", "timing/value shifts", "independent author fixtures"],
                "targets": {"motif_ap": .85, "surge_ap": .85, "stretch_ap": .90, "p100": .90},
                "status": "NOT RUN"}, stream, indent=2)
        return 0
    if args.command == "promote":
        manifest, models = candidate.load(args.artifact, args.manifest_sha256)
        approval = json.loads(args.approval.read_text())
        for key in ("representative_labels", "domain", "decision_reason", "approved_by", "label_provenance", "validation_reports", "limitations"):
            if not approval.get(key):
                parser.error(f"promotion requires explicit owner applicability judgement: {key}")
        candidate.save(args.output, models, name=manifest["model"], eligibility="validated_candidate", promotion=approval,
            provenance={**manifest["provenance"], "promotion_approval_sha256": candidate.sha(args.approval), "parent_manifest_sha256": args.manifest_sha256})
        return 0
    protocol = json.loads(args.protocol.read_text())
    selection = args.protocol.with_name(args.protocol.name + ".frozen.json")
    if args.command == "freeze":
        manifest, _ = candidate.load(args.artifact, args.manifest_sha256)
        with selection.open("x") as stream:
            json.dump({"manifest_sha256": args.manifest_sha256, "release_id": manifest["release_id"],
                "protocol_sha256": candidate.sha(args.protocol), "validation_report_sha256": candidate.sha(args.validation_report),
                "reason": args.reason, "inference_adaptation": "NONE"}, stream, indent=2)
        return 0
    finals = protocol["reserved_finals"]
    if args.command in {"train", "compare"}:
        if selection.exists() or list(args.protocol.parent.glob(args.protocol.name + ".*.evaluated")):
            parser.error("selection is frozen/finals consumed; register a genuinely new protocol/finals for a new study")
        paths = [getattr(args, split).resolve() for split in ("training", "calibration", "validation")]
        truth_paths = [getattr(args, split + "_truth").resolve() for split in ("training", "calibration", "validation")]
        if len(set(paths)) != 3 or len(set(truth_paths)) != 3 or any(path == Path(final) or Path(final) in path.parents for path in paths + truth_paths for final in finals):
            parser.error("training/calibration/validation must be distinct and cannot use reserved finals")
        research_admission(paths)
        populations = [population(path, getattr(args, split + "_truth"), args.exclude_family) for path, split in zip(paths, ("training", "calibration", "validation"), strict=True)]
        overlap = {f"{a}/{b}": len(set(populations[a][2]) & set(populations[b][2])) for a, b in ((0, 1), (0, 2), (1, 2))}
        args.output.mkdir(parents=True, exist_ok=False)
        results = {}
        for name in (["hist", "xgboost", "lightgbm", "hybrid"] if args.command == "compare" else [args.model]):
            try:
                models = candidate.train(populations[0][0], populations[0][1], populations[1][0], populations[1][1], name=name)
                manifest = candidate.save(args.output / name, models, name=name, provenance={
                    "protocol_sha256": candidate.sha(args.protocol), "split_group_overlap": overlap,
                    "procedure_code_sha256": {str(path.relative_to(Path(__file__).resolve().parents[1])): candidate.sha(path)
                        for path in (Path(__file__).resolve(), Path(candidate.__file__).resolve(), Path(__file__).resolve().parents[1] / "app/ml/grains.py")},
                    "entity_split_audit": "NOT VERIFIED by scenario IDs alone; require independent address/entity overlap audit for domain validation",
                    "training_feature_baselines": dict(zip(candidate.COLUMNS, map(float, np.median(populations[0][0], axis=0)), strict=True)),
                    "label_prevalence": {split: {task: float(values.mean()) for task, values in pop[1].items()}
                        for split, pop in zip(("training", "calibration"), populations[:2], strict=True)},
                    "truth_sha256": {split: candidate.sha(getattr(args, split + "_truth")) for split in ("training", "calibration", "validation")},
                    "label_meaning": "motif/surge/contrast propositions, NOT criminality", "label_domain": args.label_domain,
                    "excluded_positive_families": args.exclude_family, "validation_used_for": "selection only"})
                results[name] = {"manifest_sha256": candidate.sha(args.output / name / "manifest.json"), "release": manifest["release_id"],
                                 "validation": metrics(candidate.infer(models, populations[2][0]), populations[2][1], groups=populations[2][2], benign=populations[2][3])}
            except ImportError as error:
                results[name] = {"status": "UNAVAILABLE", "error": str(error)}
        (args.output / "comparison.json").write_text(json.dumps({"models": results, "split_overlap": overlap, "promotion": "NONE; explicit representative-label decision required"}, indent=2))
        return int(all(result.get("status") == "UNAVAILABLE" for result in results.values()))
    if str(args.dataset.resolve()) not in finals:
        parser.error("transfer evaluation requires a newly registered final")
    frozen_identity = json.loads(selection.read_text())
    if frozen_identity["manifest_sha256"] != args.manifest_sha256 or frozen_identity["protocol_sha256"] != candidate.sha(args.protocol):
        parser.error("evaluation candidate/protocol differs from frozen validation selection")
    manifest, models = candidate.load(args.artifact, args.manifest_sha256)
    import hashlib
    final_id = hashlib.sha256(str(args.dataset.resolve()).encode()).hexdigest()[:16]
    marker = args.protocol.with_name(args.protocol.name + "." + final_id + ".evaluated")
    with marker.open("x") as stream:
        stream.write("Consumed final: frozen transfer, no refitting/selection\n")
    matrix, labels, groups, benign, families = population(args.dataset, args.truth, return_families=True)
    scores = candidate.infer(models, matrix)
    frozen = metrics(scores, labels, budget=args.review_budget, groups=groups, benign=benign)
    strata = {}
    for family in sorted(set(families)):
        mask = np.asarray(families) == family
        strata[family] = metrics({task: values[mask] for task, values in scores.items()},
            {task: values[mask] for task, values in labels.items()}, budget=args.review_budget,
            groups=np.asarray(groups)[mask], benign=benign[mask])
    previous = metrics(baseline(args.dataset, args.truth), labels, budget=args.review_budget, groups=groups, benign=benign)
    # Baseline has uncalibrated z-scores; probability metrics/.5 confusion are invalid.
    for row in previous.values():
        for field in ("brier", "confusion", "precision", "recall", "benign_false_positives"):
            row.pop(field, None)
        row["calibration"] = "not a pattern probability; baseline z-score thresholds not equated to .5"
    with args.output.open("x") as stream:
        json.dump({"procedure": "frozen fitted-weight transfer; no test-label fitting or adaptation", "model": manifest,
            "dataset": args.dataset.name, "truth_sha256": candidate.sha(args.truth), "protocol_sha256": candidate.sha(args.protocol),
            "results": frozen, "strata": strata, "baseline": {"procedure": "v2 label-free per-dataset reference refit; retrospective D; not frozen transfer", "results": previous},
            "candidate": {"procedure": "frozen transfer; no inference adaptation"},
            "comparison": {task: {"ap_delta": frozen[task]["ap"] - previous[task]["ap"] if frozen[task]["ap"] is not None and previous[task]["ap"] is not None else None} for task in candidate.TASKS}}, stream, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
