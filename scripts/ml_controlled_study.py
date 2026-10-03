"""Pre-registered synthetic research grid; never fits or deploys a case model."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from threadpoolctl import threadpool_limits

from app.ml import evaluate as ev
from app.ml import grains, layers
from app.ml.facts import assign_splits, load_facts, split_boundaries
from app.ml.findings import A_SCORE_WITH, release_identity, release_manifest_sha256
from app.ml.fusion import stouffer_fuse

SEED = 42
TARGET = "synthetic-pattern-majority-v1"


def stress_facts(facts, mode):
    """Registered observable perturbations, independent of truth labels."""
    if not mode:
        return facts
    times = facts.tx_time.copy()
    previous = facts.in_prev.copy()
    network = {name: getattr(facts, name).copy() for name in ("tx_src_ip", "tx_asn", "tx_country")}
    if mode == "missing_network_prevouts":
        for values in network.values():
            values[::5] = -1
        resolved = np.flatnonzero(previous >= 0)
        previous[resolved[::20]] = -1
    elif mode == "timing_endpoint_skew":
        times = times // 60 * 60
        network["tx_src_ip"][::4] = 0
        network["tx_asn"][::4] = 0
        network["tx_src_ip"][::20] = -1
    else:
        raise ValueError("unregistered stress construction")
    return replace(facts, tx_time=times, in_prev=previous, **network)


def episode_groups(facts, truth):
    """Scenario/episode clusters, otherwise recipient entity; no truth features."""
    groups = np.asarray([f"tx:{i}" for i in range(facts.transaction_count)], dtype=object)
    assigned = np.zeros(facts.transaction_count, dtype=bool)
    for tx, address in zip(facts.out_tx, facts.out_addr, strict=True):
        if address >= 0 and not assigned[tx]:
            groups[tx] = f"entity:{address}"
            assigned[tx] = True
    for txid, record in truth["labelled_transactions"].items():
        slot = facts.tx_index.get(txid)
        if slot is not None and record.get("episode_id"):
            groups[slot] = "episode:" + str(record["episode_id"])
    for scenario in truth.get("scenarios", []):
        for txid in scenario.get("transaction_ids", []):
            slot = facts.tx_index.get(txid)
            if slot is not None:
                groups[slot] = "scenario:" + scenario["scenario_id"]
    return groups


def cluster_interval(score, positive, mask, groups, replicates=100):
    values, truth, clusters = score[mask], positive[mask], groups[mask]
    unique, inverse = np.unique(clusters, return_inverse=True)
    if unique.size < 2 or not truth.any():
        return {"status": "insufficient clusters/classes", "clusters": int(unique.size)}
    order = np.argsort(inverse, kind="stable")
    offsets = np.r_[0, np.cumsum(np.bincount(inverse))]
    members = [order[offsets[i]:offsets[i + 1]] for i in range(unique.size)]
    rng, samples = np.random.default_rng(SEED), []
    for _ in range(replicates):
        indices = np.concatenate([members[i] for i in rng.integers(0, unique.size, unique.size)])
        if truth[indices].any():
            ranking = np.argsort(-values[indices], kind="stable")[:100]
            samples.append((ev._average_precision(values[indices], truth[indices]), float(truth[indices][ranking].mean())))
    array = np.asarray(samples)
    return {"method": "scenario/episode/entity cluster percentile bootstrap; conditional on fitted model",
            "clusters": int(unique.size), "replicates": len(samples), "seed": SEED,
            "ap_95": np.quantile(array[:, 0], [.025, .975]).tolist(),
            "p_at_100_95": np.quantile(array[:, 1], [.025, .975]).tolist()}


def split_audit(facts, splits, groups):
    result = {}
    populations = {}
    for name, index in (("reference", 0), ("validation", 1), ("final", 2)):
        mask = splits == index
        entities = set(facts.out_addr[mask[facts.out_tx]].tolist()) - {-1}
        populations[name] = (set(groups[mask]), entities)
        result[name] = {"transactions": int(mask.sum()), "time_min": int(facts.tx_time[mask].min()) if mask.any() else None,
                        "time_max": int(facts.tx_time[mask].max()) if mask.any() else None}
    result["overlap"] = {f"{a}/{b}": {"clusters": len(populations[a][0] & populations[b][0]),
                                      "recipient_entities": len(populations[a][1] & populations[b][1])}
                         for a, b in (("reference", "validation"), ("reference", "final"), ("validation", "final"))}
    return result


def time_groups(facts):
    order = np.argsort(facts.tx_time, kind="stable")
    times = facts.tx_time[order]
    if not times.size:
        return
    boundaries = np.append(np.flatnonzero(np.diff(times)), times.size - 1)
    start = 0
    for stop in boundaries + 1:
        yield int(times[start]), order[start:stop]
        start = stop


def causal_history(facts):
    """Strictly earlier recipient activity/value/gap, excluding equal-time peers."""
    starts, order = facts.outputs_of()
    result = np.zeros((facts.transaction_count, 4), dtype=np.float32)
    state = {}
    for observed, txs in time_groups(facts):
        pending = []
        for tx in txs:
            outs = order[starts[tx]:starts[tx + 1]]
            addresses = np.unique(facts.out_addr[outs])
            addresses = addresses[addresses >= 0]
            prior = [state.get(int(a), (0, 0, None)) for a in addresses]
            if prior:
                result[tx, 0] = np.log1p(sum(p[0] for p in prior))
                result[tx, 1] = np.log1p(sum(p[1] for p in prior))
                gaps = [int(observed) - p[2] for p in prior if p[2] is not None]
                result[tx, 2] = np.log1p(max(gaps, default=0))
                result[tx, 3] = sum(p[0] > 0 for p in prior) / len(prior)
            for address in addresses:
                value = int(facts.out_value[outs][facts.out_addr[outs] == address].sum())
                pending.append((int(address), value))
        for address, value in pending:
            count, total, _ = state.get(address, (0, 0, None))
            state[address] = count + 1, total + value, int(observed)
    return result


def causal_burst_raw(facts, flags):
    """Use only the last completed bucket; v2's containing-bucket score is retrospective."""
    from app.ml.detectors import poisson_upper_surprisal

    raw = np.zeros(facts.transaction_count, dtype=np.float64)
    for flag in flags.values():
        _, counts, bucket = grains.build_motif_series(facts, flag)
        # The legacy EWMA seeds its rate with the first eight buckets. That
        # leaks future counts into early rows even if projection is lagged.
        # Research uses a fixed prior and strictly past completed counts.
        baseline, running = np.empty_like(counts), .25
        decay = .5 ** (1 / 96)
        for index, value in enumerate(counts):
            baseline[index] = max(.25, running)
            running = decay * running + (1 - decay) * value
        values = poisson_upper_surprisal(counts, baseline)
        valid = (bucket > 0) & (flag > 0)
        raw[valid] = np.maximum(raw[valid], values[bucket[valid] - 1])
    return raw


def causal_burst(facts, flags, reference):
    return layers._rank_normalise(causal_burst_raw(facts, flags), reference)


def feature_groups(facts, table, a_score, d_score):
    graph = grains.build_graph_table(facts).matrix[:, :10].copy()
    # Exclude simultaneous recipients: the historical graph comparator uses a
    # deterministic peer order, which is not strictly-earlier history.
    starts, order = facts.outputs_of()
    prior = {}
    for _, txs in time_groups(facts):
        pending = []
        for tx in txs:
            addresses = facts.out_addr[order[starts[tx]:starts[tx + 1]]]
            counts = [prior.get(int(a), 0) for a in addresses if a >= 0]
            graph[tx, 7:10] = (np.mean(counts) if counts else 0, max(counts, default=0),
                               np.mean(np.asarray(counts) > 0) if counts else 0)
            pending.extend(int(a) for a in addresses if a >= 0)
        for address in pending:
            prior[address] = prior.get(address, 0) + 1
    network = grains.build_network_context(facts)
    network_matrix = np.full((facts.transaction_count, 3), np.nan, dtype=np.float32)
    if network is not None:
        network_matrix[:, :2] = network.matrix[:, :2]
        # Country rarity uses only classes observed strictly earlier, avoiding
        # the global final-snapshot vocabulary size in the older comparator.
        counts, total = {}, 0
        for observed, txs in time_groups(facts):
            for tx in txs:
                country = int(facts.tx_country[tx])
                if country >= 0:
                    network_matrix[tx, 2] = -np.log((counts.get(country, 0) + 1) / (total + max(1, len(counts))))
            for tx in txs:
                country = int(facts.tx_country[tx])
                if country >= 0:
                    counts[country] = counts.get(country, 0) + 1
                    total += 1
        missing = facts.tx_src_ip < 0
        network_matrix[missing, 0] = np.nan
        network_matrix[facts.tx_asn < 0, 1] = np.nan
    return {"structure_age": (table.matrix, table.columns),
            "prior_history": (causal_history(facts), ("prior_activity_log", "prior_value_log", "prior_gap_log", "prior_reuse_share")),
            "causal_graph": (graph, grains.GRAPH_COLUMNS[:10]),
            "prior_network": (network_matrix, ("endpoint_trailing", "asn_trailing", "country_rarity_prior")),
            "a_d_context": (np.column_stack([a_score, d_score]), ("A_global", "D_completed_bucket"))}


def causal_facts(facts):
    """A claimed prevout at the same/future timestamp is not prior evidence."""
    previous = facts.in_prev.copy()
    resolved = np.flatnonzero(previous >= 0)
    parent_time = facts.tx_time[facts.out_tx[previous[resolved]]]
    previous[resolved[parent_time >= facts.tx_time[facts.in_tx[resolved]]]] = -1
    return replace(facts, in_prev=previous)


def reference_masks(times, reference):
    """Chronological fit/calibration split, regardless of file order; never split a timestamp."""
    indices = np.flatnonzero(reference)
    if indices.size < 2:
        raise ValueError("insufficient reference rows for a separate calibration period")
    ordered = indices[np.argsort(times[indices], kind="stable")]
    boundary = times[ordered[min(ordered.size - 1, max(1, int(ordered.size * .8)))]]
    train = reference & (times < boundary)
    calibration = reference & (times >= boundary)
    if not train.any() or not calibration.any():
        raise ValueError("insufficient distinct reference timestamps for chronological calibration")
    return train, calibration


def reliability(probability, labels):
    bins, ece = [], 0.0
    if not labels.size:
        return {"samples": 0, "status": "unavailable"}
    for index in range(10):
        mask = (probability >= index / 10) & (probability <= 1 if index == 9 else probability < (index + 1) / 10)
        count = int(mask.sum())
        if count:
            predicted, observed = float(probability[mask].mean()), float(labels[mask].mean())
            ece += count / labels.size * abs(predicted - observed)
            bins.append({"lower": index / 10, "upper": (index + 1) / 10, "samples": count,
                         "mean_prediction": predicted, "positive_fraction": observed})
    return {"samples": int(labels.size), "brier": float(np.mean((probability - labels) ** 2)), "ece": ece, "bins": bins}


def metrics(score, positive, mask, threshold, *, budget=.01):
    values, truth = score[mask], positive[mask]
    if not values.size:
        return {"status": "unavailable", "rows": 0}
    order = np.argsort(-values, kind="stable")
    k = min(100, values.size)
    capacity = int(np.floor(values.size * budget))
    selected = order[:capacity]
    flags = values >= threshold
    tp, fp = int((flags & truth).sum()), int((flags & ~truth).sum())
    fn, tn = int((~flags & truth).sum()), int((~flags & ~truth).sum())
    precision, recall = tp / max(1, tp + fp), tp / max(1, tp + fn)
    return {"rows": int(values.size), "positives": int(truth.sum()), "prevalence": float(truth.mean()),
            "ap": float(ev._average_precision(values, truth)) if truth.any() else None,
            "p_at_100": float(truth[order[:k]].mean()), "actual_k": k, "review_capacity": capacity,
            "precision_at_capacity": float(truth[selected].mean()) if capacity else None,
            "recall_at_capacity": float(truth[selected].sum() / max(1, truth.sum())),
            "threshold": float(threshold), "threshold_precision": precision, "threshold_recall": recall,
            "threshold_f1": 2 * precision * recall / max(1e-12, precision + recall), "threshold_flagged": tp + fp,
            "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn}, "ranking_ties": "stable time/transaction order"}


def study(dataset: Path, *, final_candidate=None, max_iter=100, stress=None, uncertainty=False,
          frozen_feature_contract=None):
    started = time.monotonic()
    manifest_path = dataset / "dataset_manifest.json"
    if not manifest_path.exists():
        manifest_path = dataset / "fixture_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source_hashes = {}
    for name in ("transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson", "evaluation_truth.json"):
        with (dataset / name).open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        expected = manifest.get("files", manifest.get("generated_files", {}))[name]["sha256"]
        if checksum != expected:
            raise ValueError(f"Research input integrity mismatch: {name}")
        source_hashes[name] = checksum
    facts = load_facts(dataset, with_network_context=True)
    truth = json.loads((dataset / "evaluation_truth.json").read_text())
    facts = stress_facts(facts, stress)
    splits = assign_splits(facts.tx_time, split_boundaries(dataset, truth))
    labels = ev.load_labels(facts, dataset / "evaluation_truth.json")
    clusters = episode_groups(facts, truth)
    held_family = labels.families.index("benign_payroll") if "benign_payroll" in labels.families else -2
    family_holdout = labels.family == held_family
    reference = splits == 0
    train, calibration = reference_masks(facts.tx_time, reference)
    validation = splits == (2 if final_candidate else 1)
    baseline_table = grains.build_transaction_table(facts)
    baseline_flags = layers.motif_family_flags(facts, baseline_table)
    research_facts = causal_facts(facts)
    table = grains.build_transaction_table(research_facts)
    flags = layers.motif_family_flags(research_facts, table)
    a = layers.layer_a_structure(table, train, stratified=False, score_with=A_SCORE_WITH).score
    d = causal_burst(facts, flags, train)
    groups = feature_groups(research_facts, table, a, d)
    columns = [f"{name}:{column}" for name, (_, names) in groups.items() for column in names]
    contract_hash = hashlib.sha256(json.dumps(columns).encode()).hexdigest()
    if frozen_feature_contract is not None and contract_hash != frozen_feature_contract:
        raise ValueError("Final feature contract differs from frozen validation selection")
    matrix = np.column_stack([values for values, _ in groups.values()]).astype(np.float32)
    a_v2 = layers.layer_a_structure(baseline_table, reference, stratified=False, score_with=A_SCORE_WITH).score
    d_v2 = layers.layer_d_burst(facts, baseline_flags, reference=reference).score
    baseline = stouffer_fuse({"A_global": a_v2, "D_burst": d_v2}, reference).score
    deterministic = layers.rule_baseline(facts, baseline_table).score
    names = ["E1_hgb", "E2_balanced_hgb", "E4_xgboost", "E5_lightgbm", "E6_hybrid"]
    names.extend("E3_without_" + group for group in groups)
    if final_candidate:
        if final_candidate not in names:
            raise ValueError("unknown frozen final candidate")
        names = [final_candidate]
    results, capabilities = {}, {}
    for task in ("motif", "surge", "discrimination"):
        positive, population = ev.task_targets(labels, task)
        fit = train & population & ~family_holdout
        cal = calibration & population & ~family_holdout
        target = validation & population
        results[task] = {"deterministic": metrics(deterministic, positive, target, .5),
                         "v2_deployed_procedure": metrics(baseline, positive, target,
                            np.quantile(baseline[reference & population], .99))}
        if uncertainty:
            results[task]["v2_deployed_procedure"]["uncertainty"] = cluster_interval(baseline, positive, target, clusters)
        if positive[fit].sum() < 10 or (~positive[fit]).sum() < 50:
            capabilities[task] = "insufficient training classes; no supervised fit"
            continue
        supervised_cache = None
        for name in names:
            included = [key for key in groups if name != "E3_without_" + key]
            features = matrix if len(included) == len(groups) else np.column_stack([groups[g][0] for g in included])
            weights = None
            if name == "E2_balanced_hgb":
                y = positive[fit]
                weights = np.where(y, y.size / (2 * y.sum()), y.size / (2 * (~y).sum()))
            try:
                if name == "E4_xgboost":
                    from xgboost import XGBClassifier

                    model = XGBClassifier(n_estimators=max_iter, max_depth=5, learning_rate=.08,
                                          tree_method="hist", n_jobs=1, random_state=SEED)
                elif name == "E5_lightgbm":
                    from lightgbm import LGBMClassifier

                    model = LGBMClassifier(n_estimators=max_iter, max_depth=5, num_leaves=31, learning_rate=.08,
                                           n_jobs=1, random_state=SEED, verbosity=-1)
                else:
                    model = HistGradientBoostingClassifier(max_iter=max_iter, max_leaf_nodes=31, learning_rate=.08,
                                                           early_stopping=False, random_state=SEED)
                if name == "E6_hybrid" and supervised_cache is not None:
                    probability = supervised_cache
                else:
                    with threadpool_limits(limits=1):
                        model.fit(features[fit], positive[fit], sample_weight=weights)
                        probability = model.predict_proba(features)[:, 1]
                if name == "E1_hgb":
                    supervised_cache = probability
                score = probability
                if name == "E6_hybrid":
                    score = (layers._rank_normalise(probability, train) + a + d) / 3
                result = metrics(score, positive, target, np.quantile(score[fit], .99))
                if positive[cal].sum() >= 10 and (~positive[cal]).sum() >= 10:
                    calibrator = IsotonicRegression(out_of_bounds="clip").fit(score[cal], positive[cal])
                    result["calibration"] = reliability(calibrator.predict(score[target]), positive[target])
                    result["calibration"]["fitted_samples"] = int(cal.sum())
                else:
                    result["calibration"] = {"status": "insufficient calibration classes"}
                result["feature_groups"] = included
                result["scenario_family_holdout"] = metrics(score, positive, target & family_holdout, np.quantile(score[fit], .99))
                if uncertainty:
                    result["uncertainty"] = cluster_interval(score, positive, target, clusters)
                result["deployment_eligible"] = False
                results[task][name] = result
            except (ImportError, OSError) as error:
                capabilities[name] = f"dependency unavailable: {error}"
    return {"dataset": str(dataset), "transactions": facts.transaction_count,
            "source_sha256": source_hashes, "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "truth_sha256": hashlib.sha256((dataset / "evaluation_truth.json").read_bytes()).hexdigest(),
            "feature_contract_sha256": contract_hash, "feature_columns": columns,
            "split_counts": {"fit": int(train.sum()), "calibration": int(calibration.sum()), "evaluation": int(validation.sum())},
            "evaluation_split": "reserved_final_holdout" if final_candidate else "validation",
            "stress": stress, "split_audit": split_audit(facts, splits, clusters),
            "scenario_family_holdout": "benign_payroll excluded from supervised fit and calibration; not a positive-family generalization test",
            "baseline_scope": "v2 retrospective containing-bucket A+D; in-sample reference rows excluded from metrics",
            "capabilities": capabilities, "results": results, "seconds": time.monotonic() - started}


def validate_frozen_selection(selection, registration_hash):
    """Validate immutable development evidence before opening a final holdout."""
    if selection["protocol_sha256"] != registration_hash:
        raise ValueError("Frozen selection protocol mismatch")
    contracts = set()
    for name, expected in selection["validation_report_sha256"].items():
        path = Path(name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen validation report integrity mismatch")
        report = json.loads(path.read_text())
        protocol = report["protocol"]
        if (protocol["release_protocol_sha256"] != registration_hash or
                protocol["release_manifest_sha256"] != release_manifest_sha256()):
            raise ValueError("Frozen baseline/protocol differs from current research procedure")
        for dataset in report["reports"]:
            if dataset["evaluation_split"] != "validation":
                raise ValueError("Only development validation can define the final contract")
            contracts.add(dataset["feature_contract_sha256"])
    if len(contracts) != 1:
        raise ValueError("Expected one consistent frozen feature contract")
    return contracts.pop()


def run(datasets, output, *, final_candidate=None, max_iter=100, release_protocol=None, frozen_selection=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError("refusing to overwrite research evidence")
    output.parent.mkdir(parents=True, exist_ok=True)
    registration = json.loads(Path(release_protocol).read_text()) if release_protocol else None
    registration_hash = hashlib.sha256(Path(release_protocol).read_bytes()).hexdigest() if release_protocol else None
    if registration and max_iter != registration["max_iterations"]:
        raise ValueError("Iteration budget differs from pre-registered procedure")
    frozen_contract = None
    if final_candidate:
        if not registration or not frozen_selection:
            raise ValueError("Final evaluation requires pre-registered protocol and frozen validation selection")
        selection = json.loads(Path(frozen_selection).read_text())
        if selection["candidate"] != final_candidate or selection["protocol_sha256"] != registration_hash:
            raise ValueError("Final candidate/protocol differs from frozen selection")
        frozen_contract = validate_frozen_selection(selection, registration_hash)
    versions = {}
    for name in ("scikit-learn", "numpy", "scipy", "xgboost-cpu", "lightgbm"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    protocol = {"version": "controlled-study-v2", "created_before_evaluation": True, "datasets": [str(p) for p in datasets],
                "release_protocol_sha256": registration_hash,
                "mode": "synthetic research/demo only", "allowed_labels": "evaluation_only/do_not_ingest truth",
                "production_adoption": "forbidden by synthetic-only study; preserve v2",
                "baseline": release_identity(), "release_manifest_sha256": release_manifest_sha256(),
                "training": "first 80% of time-ordered reference; remaining 20% separate calibration; validation selects",
                "frozen_final_candidate": final_candidate, "max_iterations_per_candidate": max_iter, "seed": SEED,
                "regression_tolerance_ap": .01, "ranking_targets": {"motif_ap": .8, "surge_ap": .8, "p_at_100": .9},
                "versions": versions}
    with output.with_suffix(".protocol.json").open("x") as handle:
        json.dump(protocol, handle, indent=2)
    reports = []
    failure = None
    try:
        for dataset in datasets:
            stress = None
            if final_candidate:
                definition = next((d for d in registration["final_datasets"] if Path(d["directory"]).resolve() == dataset.resolve()), None)
                if definition is None:
                    raise ValueError("Dataset not reserved by release protocol")
                reserved = json.loads((dataset / ".final_reservation.json").read_text())
                manifest = json.loads((dataset / "dataset_manifest.json").read_text())
                if (reserved["protocol_sha256"] != registration_hash or manifest["seed"] != definition["seed"] or
                        reserved["selection_sha256"] != hashlib.sha256(Path(frozen_selection).read_bytes()).hexdigest()):
                    raise ValueError("Final dataset reservation or seed mismatch")
                stress = definition["stress"]
                # An exclusive marker prevents accidental repeated final evaluation.
                marker = dataset / ".reserved_final_evaluation.json"
                with marker.open("x") as handle:
                    json.dump({"candidate": final_candidate, "protocol": str(output.with_suffix('.protocol.json'))}, handle)
            reports.append(study(dataset, final_candidate=final_candidate, max_iter=max_iter, stress=stress,
                                 uncertainty=bool(registration), frozen_feature_contract=frozen_contract))
            print(json.dumps({"dataset": str(dataset), "seconds": reports[-1]["seconds"],
                              "results": {task: {name: result.get("ap") for name, result in values.items()}
                                          for task, values in reports[-1]["results"].items()}}, indent=2), flush=True)
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
        raise
    finally:
        with output.open("x") as handle:
            json.dump({"protocol": protocol, "reports": reports, "error": failure,
                       "adoption": "retain anomaly-stack-v2"}, handle, indent=2)
    return reports
