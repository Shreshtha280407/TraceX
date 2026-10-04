"""Registered frozen-weight training/comparison and transfer evaluation.

Inputs are canonical fixture directories; truth is opened ONLY in this offline
command, never in product imports. No large study runs without an explicit call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, confusion_matrix

from app.engine.investigations import PROCEDURE_SHA256
from app.engine.investigations import VERSION as GROUPING_VERSION
from app.ml import candidate
from app.ml.evaluation_identity import final_id, fingerprint
from app.ml.facts import load_facts


def population(path, truth_path, exclude_families=(), *, return_families=False, return_scope=False, contract=candidate.CONTRACT):
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
    # Local ep-001 IDs from independent seeds are not the same episode. Scope
    # to the actual canonical TX population, not directory basenames.
    population_id = hashlib.sha256("\n".join(sorted(facts.txids)).encode()).hexdigest()
    groups = [population_id + ":" + str(r.get("episode_id") or r.get("scenario_id") or facts.txids[i]) for r, i in zip(rows, selected, strict=True)]
    benign = np.asarray([bool(r.get("benign_control") or str(r.get("family", "")).startswith("benign")
        or r.get("family") in {"nearmiss_rule_positive", "batch_fanout", "consolidation", "nearmiss_batch"}) for r in rows])
    result = candidate.features(facts, contract=contract)[selected], targets, groups, benign
    if return_families:
        result = (*result, [str(row.get("family", "unknown")) for row in rows])
    if return_scope:
        result = (*result, {"eligible_transactions": facts.transaction_count, "labelled_transactions": len(selected),
            "population_scope": "all time-eligible canonical transactions"})
    return result


def research_admission(paths):
    """Streaming count screen before Python dictionaries/global research arrays."""
    from app.resources import admit_global_allocation
    total = 0
    for directory in paths:
        for name, per_row in (("transactions", 2200), ("outputs", 500), ("inputs", 200)):
            with (Path(directory) / (name + ".ndjson")).open() as stream:
                total += sum(bool(line.strip()) for line in stream) * per_row
    admit_global_allocation("candidate research retained populations/fitting", total * 2 + (512 << 20))


def baseline(dataset, truth_path, *, exclude_families=(), finding_budget=.01, return_scope=False):
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
    fused = stouffer_fuse({"A_global": a.score, "D_burst": d.score}, reference, budget=finding_budget)
    score = fused.score
    truth = json.loads(Path(truth_path).read_text())["labelled_transactions"]
    selected = sorted((i for i, txid in enumerate(facts.txids) if txid in truth and truth[txid].get("family") not in exclude_families), key=lambda i: facts.txids[i])
    scores = {task: score[selected] for task in candidate.TASKS}
    if return_scope:
        return scores, {"finding_budget_fraction": finding_budget,
            "baseline_available_findings": int(np.count_nonzero(score >= fused.threshold)),
            "candidate_available_findings": max(1, int(np.ceil(facts.transaction_count * finding_budget)))}
    return scores


def metrics(scores, labels, *, budget=100, groups=None, benign=None):
    results = {}
    rng = np.random.default_rng(42)
    for task in candidate.TASKS:
        y, score = np.asarray(labels[task], bool), np.asarray(scores[task])
        if not len(y):
            results[task] = {"status": "no labelled observations", "population": 0, "review_budget": budget,
                **{field: None for field in ("ap", "p_at_100", "p_at_review_budget", "recall_at_review_budget", "benign_false_positives")}}
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


def queue_comparison(scores, previous, labels, families, benign, budget, *, contract=candidate.CONTRACT, population_scope=None):
    """Actual versioned candidate queue policy vs v2 ranking, at the SAME K.

    Truth/family/benign flags are evaluation-only, never inference inputs.
    Structural matches in benign controls remain matches, not criminality labels.
    """
    y = np.asarray(labels["discrimination"], bool)
    # Evaluation truth schema only, never an inference-time allowlist. Purchase
    # and consolidation rows are both merchant controls in the generator.
    merchant = np.asarray([family.startswith("benign_merchant") or family == "benign_purchase" for family in families], bool) & np.asarray(benign, bool)
    reasons = []
    population_scope = population_scope or {"eligible_transactions": len(y), "labelled_transactions": len(y),
        "population_scope": "provided complete score universe"}
    if population_scope["labelled_transactions"] != population_scope["eligible_transactions"]:
        reasons.append("Truth does not cover all eligible canonical transactions; labelled-subset ranking is not the deployed queue. Unlabelled transactions are not negatives.")
    if population_scope.get("population_scope") == "all time-eligible canonical transactions":
        for field in ("candidate_available_findings", "baseline_available_findings"):
            available = population_scope.get(field)
            if not isinstance(available, int) or isinstance(available, bool) or available < budget:
                reasons.append(f"{field} is unknown or below reviewer capacity; the materialized product queue cannot exercise this K.")
    if budget <= 0 or len(y) < budget:
        reasons.append("A positive review budget supported by the labelled population is required.")
    if not y.any():
        reasons.append("No positive review-interest labels; recall/AP cannot be evaluated.")
    if not merchant.any():
        reasons.append("No labelled benign merchant controls; merchant false positives cannot be evaluated.")
    result = {"status": "NOT EVALUABLE" if reasons else "EVALUATED", "reasons": reasons,
        **population_scope,
        "population": len(y), "review_budget": budget, "merchant_controls": int(merchant.sum()),
        "unit": "labelled transaction at fixed review capacity, NOT historical episode-touch rate",
        "target": "discrimination review-interest proposition, NOT criminality or structural-match correctness",
        "candidate_policy": candidate.QUEUE_POLICIES[contract],
        "baseline_policy": "v2 fused ranking; label-free per-dataset refit, retrospective burst",
        "scope": "ranking comparison; actual findings availability and immutable source replay require product-path verification"}
    if reasons:
        return result
    orders = {}
    for name, score in (("candidate", candidate.queue_priority(scores, contract=contract)), ("baseline", previous["discrimination"])):
        order = np.argsort(-score, kind="stable")[:budget]  # population is TX-ID sorted.
        mask = np.zeros(len(y), dtype=bool)
        mask[order] = True
        orders[name] = mask
        result[name] = {"precision": float(y[order].mean()), "recall": int(y[order].sum()) / int(y.sum()),
            "benign_in_queue": int(np.asarray(benign, bool)[order].sum()),
            "merchant_false_positives": int(merchant[order].sum()),
            "merchant_false_positive_rate": int(merchant[order].sum()) / int(merchant.sum())}
    result["delta"] = {"precision": result["candidate"]["precision"] - result["baseline"]["precision"],
        "recall": result["candidate"]["recall"] - result["baseline"]["recall"],
        "merchant_false_positive_reduction": result["baseline"]["merchant_false_positives"] - result["candidate"]["merchant_false_positives"],
        "merchant_removed_from_queue": int((merchant & orders["baseline"] & ~orders["candidate"]).sum()),
        "merchant_new_in_queue": int((merchant & orders["candidate"] & ~orders["baseline"]).sum())}
    return result


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
        train_parser.add_argument("--review-budget", type=int, default=100)
        train_parser.add_argument("--finding-budget", type=float, default=.01, help="Product retained-finding fraction; must match TRACEX_ML_REVIEW_BUDGET for deployment")
    transfer = sub.add_parser("evaluate")
    transfer.add_argument("--protocol", type=Path, required=True)
    transfer.add_argument("--artifact", type=Path, required=True)
    transfer.add_argument("--manifest-sha256", required=True)
    transfer.add_argument("--dataset", type=Path, required=True)
    transfer.add_argument("--truth", type=Path, required=True)
    transfer.add_argument("--output", type=Path, required=True)
    transfer.add_argument("--review-budget", type=int, default=100)
    transfer.add_argument("--finding-budget", type=float, default=.01, help="Product retained-finding fraction; must match TRACEX_ML_REVIEW_BUDGET for deployment")
    transfer.add_argument("--group-quality-result", type=Path, help="Actual same-source frozen-worker group queue evaluation; NOT inferred from transaction labels")
    promote = sub.add_parser("promote", help="OWNER decision only: never automatically promotes from a label count/AP")
    promote.add_argument("--artifact", type=Path, required=True)
    promote.add_argument("--manifest-sha256", required=True)
    promote.add_argument("--approval", type=Path, required=True)
    promote.add_argument("--protocol", type=Path, required=True, help="Registered protocol with its immutable .frozen.json source registry")
    promote.add_argument("--quality-result", type=Path, action="append", required=True, help="Frozen transfer reports for these weights; measured matched-capacity product queue is required")
    promote.add_argument("--output", type=Path, required=True)
    freeze = sub.add_parser("freeze", help="Pin validation-selected weights before opening any fresh final")
    freeze.add_argument("--protocol", type=Path, required=True)
    freeze.add_argument("--artifact", type=Path, required=True)
    freeze.add_argument("--manifest-sha256", required=True)
    freeze.add_argument("--validation-report", type=Path, required=True)
    freeze.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command in {"train", "compare", "evaluate"} and not (args.review_budget > 0 and 0 < args.finding_budget <= 1):
        parser.error("review-budget must be positive and finding-budget must be in (0,1]")
    if args.command == "register":
        finals = [str(Path(value).resolve()) for value in args.final]
        if len(set(finals)) != len(finals) or any(Path(value).exists() for value in finals):
            parser.error("reserve fresh, distinct final paths before generation/selection; evaluated holdouts cannot be reused")
        args.protocol.parent.mkdir(parents=True, exist_ok=True)
        with args.protocol.open("x") as stream:
            json.dump({"schema": 2, "reserved_finals": finals, "registered_final_ids": [final_id(p) for p in finals],
                "grouping_version": GROUPING_VERSION, "grouping_sha256": PROCEDURE_SHA256,
                "group_target": "Connected observed pattern episode; no automatic any-positive group label",
                "group_queue_policy": "group-family-round-robin-v1", "group_capacity": 100,
                "selection": "validation only; no final labels enter fitting",
                "feature_contract": candidate.CONTRACT, "grid": ["hist", "xgboost", "lightgbm", "hybrid"],
                "generalization": ["fresh seeds/prevalence", "unseen pattern families", "payroll/merchant/exchange/batching/consolidation", "missing/noisy IP/ASN", "incomplete prevouts", "degree/reuse skew", "timing/value shifts", "independent author fixtures"],
                "targets": {"motif_ap": .85, "surge_ap": .85, "stretch_ap": .90, "p100": .90},
                "status": "NOT RUN"}, stream, indent=2)
        return 0
    if args.command == "promote":
        from app.ml.promotion import assess
        manifest, _ = candidate.load(args.artifact, args.manifest_sha256)
        approval = json.loads(args.approval.read_text())
        for key in ("representative_labels", "domain", "decision_reason", "approved_by", "label_provenance", "validation_reports", "limitations"):
            if not approval.get(key):
                parser.error(f"promotion requires explicit owner applicability judgement: {key}")
        frozen = json.loads(args.protocol.with_name(args.protocol.name + ".frozen.json").read_text())
        if frozen.get("protocol_sha256") != candidate.sha(args.protocol) or frozen.get("manifest_sha256") != args.manifest_sha256:
            parser.error("promotion protocol/manifest differs from pinned frozen selection")
        quality = assess(manifest, [json.loads(path.read_text()) for path in args.quality_result], frozen=frozen)
        quality["reports_sha256"] = [candidate.sha(path) for path in args.quality_result]
        decision = args.output.with_name(args.output.name + ".promotion-decision.json")
        decision.parent.mkdir(parents=True, exist_ok=True)
        with decision.open("x") as stream:
            json.dump(quality, stream, indent=2)
        if quality["status"] != "PASSED":
            parser.error(f"promotion blocked; dataset-specific reasons saved in {decision}")
        approval = {**approval, "quality_validation": quality}
        candidate.promote_verified(args.artifact, args.output, args.manifest_sha256, approval, candidate.sha(args.approval))
        return 0
    protocol = json.loads(args.protocol.read_text())
    selection = args.protocol.with_name(args.protocol.name + ".frozen.json")
    if args.command == "freeze":
        manifest, _ = candidate.load(args.artifact, args.manifest_sha256)
        if manifest.get("provenance", {}).get("protocol_sha256") != candidate.sha(args.protocol):
            parser.error("artifact protocol provenance does not match registered protocol")
        # Hash sources without opening truth labels. Freeze before any final
        # evaluation; future substituted files cannot masquerade as the final.
        registry = {}
        for p in protocol["reserved_finals"]:
            root = Path(p)
            truth_path = root / ("ground_truth.json" if (root / "ground_truth.json").exists() else "evaluation_truth.json")
            registry[final_id(p)] = {**fingerprint(p), "truth_sha256": candidate.sha(truth_path)}
        with selection.open("x") as stream:
            json.dump({"manifest_sha256": args.manifest_sha256, "release_id": manifest["release_id"],
                "protocol_sha256": candidate.sha(args.protocol), "validation_report_sha256": candidate.sha(args.validation_report),
                "final_registry": registry, "grouping_sha256": protocol.get("grouping_sha256"),
                "review_budget": manifest.get("provenance", {}).get("review_budget"),
                "finding_budget_fraction": manifest.get("provenance", {}).get("finding_budget_fraction"),
                "reason": args.reason, "inference_adaptation": "NONE"}, stream, indent=2)
        return 0
    finals = protocol["reserved_finals"]
    if args.command in {"train", "compare"}:
        if protocol.get("grouping_sha256") != PROCEDURE_SHA256:
            parser.error("grouping procedure changed; reserve a genuinely new evaluation protocol before selection")
        if selection.exists() or list(args.protocol.parent.glob(args.protocol.name + ".*.evaluated")):
            parser.error("selection is frozen/finals consumed; register a genuinely new protocol/finals for a new study")
        paths = [getattr(args, split).resolve() for split in ("training", "calibration", "validation")]
        truth_paths = [getattr(args, split + "_truth").resolve() for split in ("training", "calibration", "validation")]
        if len(set(paths)) != 3 or len(set(truth_paths)) != 3 or any(path == Path(final) or Path(final) in path.parents for path in paths + truth_paths for final in finals):
            parser.error("training/calibration/validation must be distinct and cannot use reserved finals")
        research_admission(paths)
        contract = protocol["feature_contract"]
        columns, _ = candidate.contract_identity(contract)
        populations = [population(path, getattr(args, split + "_truth"), args.exclude_family, return_families=True, return_scope=True, contract=contract)
            for path, split in zip(paths, ("training", "calibration", "validation"), strict=True)]
        # Same exclusion as validation; baseline() returns all labelled TXs.
        validation_previous, retained_scope = baseline(args.validation, args.validation_truth,
            exclude_families=args.exclude_family, finding_budget=args.finding_budget, return_scope=True)
        populations[2][5].update(retained_scope)
        overlap = {f"{a}/{b}": len(set(populations[a][2]) & set(populations[b][2])) for a, b in ((0, 1), (0, 2), (1, 2))}
        args.output.mkdir(parents=True, exist_ok=False)
        results = {}
        for name in (["hist", "xgboost", "lightgbm", "hybrid"] if args.command == "compare" else [args.model]):
            model_started = time.monotonic()
            try:
                models = candidate.train(populations[0][0], populations[0][1], populations[1][0], populations[1][1], name=name, contract=contract)
                manifest = candidate.save(args.output / name, models, name=name, provenance={
                    "protocol_sha256": candidate.sha(args.protocol), "split_group_overlap": overlap,
                    "registered_final_ids": protocol["registered_final_ids"], "grouping_sha256": PROCEDURE_SHA256,
                    "grouping_version": GROUPING_VERSION, "group_capacity": 100,
                    "review_budget": args.review_budget, "finding_budget_fraction": args.finding_budget,
                    "procedure_code_sha256": {str(path.relative_to(Path(__file__).resolve().parents[1])): candidate.sha(path)
                        for path in (Path(__file__).resolve(), Path(candidate.__file__).resolve(), Path(__file__).resolve().parents[1] / "app/ml/grains.py",
                            Path(__file__).resolve().parents[1] / "app/ml/recipient_history.py")},
                    "entity_split_audit": "NOT VERIFIED by scenario IDs alone; require independent address/entity overlap audit for domain validation",
                    "training_feature_baselines": dict(zip(columns, map(float, np.median(populations[0][0], axis=0)), strict=True)),
                    "label_prevalence": {split: {task: float(values.mean()) for task, values in pop[1].items()}
                        for split, pop in zip(("training", "calibration"), populations[:2], strict=True)},
                    "truth_sha256": {split: candidate.sha(getattr(args, split + "_truth")) for split in ("training", "calibration", "validation")},
                    "label_meaning": "motif/surge/contrast propositions, NOT criminality", "label_domain": args.label_domain,
                    "excluded_positive_families": args.exclude_family, "validation_used_for": "selection only"}, contract=contract)
                scores = candidate.infer(models, populations[2][0])
                results[name] = {"manifest_sha256": candidate.sha(args.output / name / "manifest.json"), "release": manifest["release_id"],
                    "validation": metrics(scores, populations[2][1], budget=args.review_budget, groups=populations[2][2], benign=populations[2][3]),
                    "queue_comparison": queue_comparison(scores, validation_previous, populations[2][1], populations[2][4], populations[2][3], args.review_budget, contract=contract, population_scope=populations[2][5])}
                from app.telemetry import peak_rss_bytes
                results[name]["resources"] = {"training_validation_seconds": time.monotonic() - model_started,
                    "process_peak_rss_bytes": peak_rss_bytes(), "peak_scope": "Cumulative process peak, including previous candidate models and baseline; not isolated per-model peak"}
            except ImportError as error:
                results[name] = {"status": "UNAVAILABLE", "error": str(error)}
        baseline_metrics = metrics(validation_previous, populations[2][1], budget=args.review_budget, groups=populations[2][2], benign=populations[2][3])
        for row in baseline_metrics.values():
            for field in ("brier", "confusion", "precision", "recall", "benign_false_positives"):
                row.pop(field, None)
        (args.output / "comparison.json").write_text(json.dumps({"models": results, "split_overlap": overlap,
            "baseline_validation": baseline_metrics,
            "baseline_limitations": "v2 label-free reference refit, retrospective; z-scores are not probabilities, so .5 confusion/Brier in the generic metric helper are inapplicable",
            "promotion": "NONE; explicit representative-label decision required"}, indent=2))
        return int(all(result.get("status") == "UNAVAILABLE" for result in results.values()))
    if str(args.dataset.resolve()) not in finals:
        parser.error("transfer evaluation requires a newly registered final")
    frozen_identity = json.loads(selection.read_text())
    if frozen_identity["manifest_sha256"] != args.manifest_sha256 or frozen_identity["protocol_sha256"] != candidate.sha(args.protocol):
        parser.error("evaluation candidate/protocol differs from frozen validation selection")
    manifest, models = candidate.load(args.artifact, args.manifest_sha256)
    approved_queue = manifest.get("provenance", {})
    if approved_queue.get("review_budget") != args.review_budget or approved_queue.get("finding_budget_fraction") != args.finding_budget:
        parser.error("evaluation capacity/retention differs from fitted frozen provenance; legacy unpinned studies need a new registered protocol, never a reinterpretation")
    identity = final_id(args.dataset)
    source = fingerprint(args.dataset)
    pinned = frozen_identity.get("final_registry", {}).get(identity, {})
    if any(pinned.get(key) != value for key,value in source.items()) or pinned.get("truth_sha256") != candidate.sha(args.truth) or frozen_identity.get("grouping_sha256") != PROCEDURE_SHA256:
        parser.error("final source substituted or frozen grouping procedure changed")
    marker = args.protocol.with_name(args.protocol.name + "." + identity + ".evaluated")
    with marker.open("x") as stream:
        stream.write("Consumed final: frozen transfer, no refitting/selection\n")
    matrix, labels, groups, benign, families, population_scope = population(args.dataset, args.truth, return_families=True, return_scope=True, contract=manifest["feature_contract"])
    scores = candidate.infer(models, matrix)
    group_quality = {"status": "NOT EVALUABLE", "reasons": ["Transaction labels alone do not establish the bounded group proposition. Evaluate the actual materialized group queue with independently declared episode truth."], "grouping_sha256": PROCEDURE_SHA256}
    if args.group_quality_result:
        group_quality = json.loads(args.group_quality_result.read_text())
        binding = {"protocol_sha256": candidate.sha(args.protocol), "final_id": identity,
                   "source_sha256": source["source_sha256"], "release_id": manifest["release_id"], "grouping_sha256": PROCEDURE_SHA256}
        if any(group_quality.get(k) != v for k,v in binding.items()):
            parser.error("group-quality report does not bind these exact frozen weights, protocol, source and grouping")
    frozen = metrics(scores, labels, budget=args.review_budget, groups=groups, benign=benign)
    strata = {}
    for family in sorted(set(families)):
        mask = np.asarray(families) == family
        strata[family] = metrics({task: values[mask] for task, values in scores.items()},
            {task: values[mask] for task, values in labels.items()}, budget=args.review_budget,
            groups=np.asarray(groups)[mask], benign=benign[mask])
    previous_scores, retained_scope = baseline(args.dataset, args.truth, finding_budget=args.finding_budget, return_scope=True)
    population_scope.update(retained_scope)
    previous = metrics(previous_scores, labels, budget=args.review_budget, groups=groups, benign=benign)
    # Baseline has uncalibrated z-scores; probability metrics/.5 confusion are invalid.
    for row in previous.values():
        for field in ("brier", "confusion", "precision", "recall", "benign_false_positives"):
            row.pop(field, None)
        row["calibration"] = "not a pattern probability; baseline z-score thresholds not equated to .5"
    with args.output.open("x") as stream:
        json.dump({"procedure": "frozen fitted-weight transfer; no test-label fitting or adaptation", "model": manifest,
            "dataset": args.dataset.name, "truth_sha256": candidate.sha(args.truth), "protocol_sha256": candidate.sha(args.protocol),
            "final_id": identity, "source_sha256": source["source_sha256"], "source_inventory": source["files"],
            "grouping_sha256": PROCEDURE_SHA256,
            "group_quality": group_quality,
            "results": frozen, "strata": strata, "baseline": {"procedure": "v2 label-free per-dataset reference refit; retrospective D; not frozen transfer", "results": previous},
            "queue_comparison": queue_comparison(scores, previous_scores, labels, families, benign, args.review_budget, contract=manifest["feature_contract"], population_scope=population_scope),
            "candidate": {"procedure": "frozen transfer; no inference adaptation"},
            "comparison": {task: {"ap_delta": frozen[task]["ap"] - previous[task]["ap"] if frozen[task]["ap"] is not None and previous[task]["ap"] is not None else None} for task in candidate.TASKS}}, stream, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
