"""Label-free, registered stress preparation and compact per-task worst-case evaluation.

No training/selection occurs here. Variants retain source labels: their changed
data coverage and task-label applicability must be reviewed, never assumed.
The independent fixture is capped at 1,000 transactions for wiring checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.ml.candidate import TASKS, sha

VARIANTS = ("missing_network", "noisy_network", "incomplete_prevouts", "reuse_degree", "timing_value")


def lines(path):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def independent_fixture(output, count, seed, role):
    if not 64 <= count <= 1000:
        raise ValueError("independent wiring fixture supports 64..1000 transactions only")
    output.mkdir(parents=True, exist_ok=False)
    rng = random.Random(seed)
    rows, labels = [], {}
    families = ("benign_payroll", "benign_merchant", "benign_exchange", "batch_fanout", "consolidation", "coinjoin_like", "peel_step", "independent_equal_outputs")
    for i in range(count):
        txid = hashlib.sha256(f"{role}-{seed}-{i}".encode()).hexdigest()
        family = families[i % len(families)]
        positive = family in {"coinjoin_like", "peel_step", "independent_equal_outputs"}
        # Different, independently authored controls; no root generator episode IDs.
        n = 3 if family in {"coinjoin_like", "independent_equal_outputs", "benign_payroll"} else 2
        amounts = [1000] * n if n == 3 else [900, 100 + rng.randrange(100)]
        stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=i * 20)
        rows.append({"txid": txid, "network": "bitcoin-regtest", "timestamp": stamp.isoformat(),
            "inputs": [], "outputs": [{"address": f"{role}-{family}-recipient-{j}", "amount_sats": amount, "script_type": "p2wpkh"} for j, amount in enumerate(amounts)],
            "fee_sats": 0, "src_ip": "8.8.8.8", "dst_ip": "1.1.1.1", "asn": "AS15169", "geo_country": "US"})
        labels[txid] = {"family": family, "scenario_id": f"{role}-{seed}-scenario-{i // 8}", "motif_positive": positive,
            "surge_positive": i % 8 in {5, 6}, "discrimination_positive": positive or i % 8 in {5, 6}, "benign_control": not positive}
    # One real multi-input spend exercises the clustering/UTXO path. Shared
    # recipient activity elsewhere remains addresses, not invented ownership.
    rows[1]["inputs"] = [{**value, "prev_txid": rows[0]["txid"], "prev_vout": j} for j, value in enumerate(rows[0]["outputs"])]
    rows[1]["fee_sats"] = sum(v["amount_sats"] for v in rows[1]["inputs"]) - sum(v["amount_sats"] for v in rows[1]["outputs"])
    with (output / "ingestion_rows.ndjson").open("x") as raw, (output / "transactions.ndjson").open("x") as txs, (output / "inputs.ndjson").open("x") as ins, (output / "outputs.ndjson").open("x") as outs:
        for row in rows:
            raw.write(json.dumps(row) + "\n")
            txs.write(json.dumps({"txid": row["txid"], "block_time": row["timestamp"], "fee_sats": row["fee_sats"]}) + "\n")
            for j, value in enumerate(row["inputs"]):
                ins.write(json.dumps({**value, "txid": row["txid"], "vin": j}) + "\n")
            for j, value in enumerate(row["outputs"]):
                outs.write(json.dumps({**value, "txid": row["txid"], "vout": j}) + "\n")
    (output / "ground_truth.json").write_text(json.dumps({"evaluation_only": True, "do_not_ingest": True, "label_meaning": "independent synthetic wiring controls, not criminality or measured product quality", "labelled_transactions": labels}))
    inventory(output, {"method": "independent small wiring fixture", "seed": seed, "role": role, "count": count})


def inventory(output, provenance):
    files = {path.name: {"sha256": sha(path), "bytes": path.stat().st_size} for path in sorted(output.iterdir()) if path.is_file()}
    (output / "quality_manifest.json").write_text(json.dumps({"files": files, "provenance": provenance}, indent=2))


def stress(source, output, variant, seed):
    output.mkdir(parents=True, exist_ok=False)
    rng = random.Random(seed)
    for name in ("transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "ingestion_rows.ndjson"):
        with (output / name).open("x") as stream:
            for index, row in enumerate(lines(source / name)):
                if variant == "missing_network":
                    for key in ("src_ip", "dst_ip", "asn", "geo_country"):
                        if key in row:
                            row[key] = None
                elif variant == "noisy_network" and name == "ingestion_rows.ndjson":
                    row.update(src_ip="203.0.113." + str(1 + rng.randrange(250)), asn="AS64512", geo_country="ZZ")
                elif variant == "incomplete_prevouts":
                    if name == "inputs.ndjson" and index % 3 == 0:
                        row.update(prev_txid=None, prev_vout=None)
                    for value in row.get("inputs", [])[::3]:
                        value.update(prev_txid=None, prev_vout=None)
                elif variant == "reuse_degree":
                    if row.get("address") is not None:
                        row["address"] = "stress-recipient-" + str(index % 7)
                    for value in row.get("outputs", []):
                        value["address"] = "stress-recipient-" + str(index % 7)
                elif variant == "timing_value":
                    for key in ("block_time", "timestamp"):
                        if row.get(key):
                            time = datetime.fromisoformat(row[key])
                            row[key] = (datetime(2026, 1, 1, tzinfo=UTC) + (time - datetime(2026, 1, 1, tzinfo=UTC)) * 2).isoformat()
                    for value in [row, *row.get("inputs", []), *row.get("outputs", [])]:
                        if value.get("amount_sats") is not None:
                            value["amount_sats"] *= 2
                        if value.get("fee_sats") is not None:
                            value["fee_sats"] *= 2
                stream.write(json.dumps(row) + "\n")
    # Truth is not consulted for perturbation; copied only after feature inputs finish.
    truth_path = source / ("ground_truth.json" if (source / "ground_truth.json").exists() else "evaluation_truth.json")
    (output / "ground_truth.json").write_bytes(truth_path.read_bytes())
    inventory(output, {"source_manifest_sha256": sha(source / "quality_manifest.json") if (source / "quality_manifest.json").exists() else None,
        "source_truth_sha256": sha(truth_path), "variant": variant, "seed": seed,
        "limitation": "source labels retained; review task-label validity after timing/value/reuse perturbation; not independent populations"})


def metric_observation(report, task, field, budget, path):
    """Never turn an absent measurement into zero or assume its applicability."""
    row = report.get("results", {}).get(task, {})
    population = row.get("population")
    known_population = isinstance(population, int) and not isinstance(population, bool) and population >= 0
    observation = {"dataset": report.get("dataset", path.stem), "report": path.name,
        "population": population, "status": "NOT EVALUABLE", "value": None}
    if not row or row.get("status") == "no labelled observations" or (known_population and population == 0):
        observation["reason"] = "No labelled observations or task results; required metrics cannot be evaluated."
        return observation
    if field == "p_at_100":
        if not known_population:
            observation["reason"] = "P@100 applicability is unknown: a labelled population count is required."
            return observation
        if population < 100:
            observation.update(status="NOT APPLICABLE", reason=f"P@100 requires at least 100 labelled observations; population is {population}.")
            return observation
    if field in {"p_at_review_budget", "recall_at_review_budget"}:
        if budget == 0:
            observation.update(status="NOT APPLICABLE", reason="Reviewer capacity is zero; reviewer-budget metrics are not applicable.")
            return observation
        if row.get("review_budget") is None:
            observation["reason"] = "Recorded reviewer capacity is missing; review-budget metrics are not comparable."
            return observation
        if known_population and population < budget:
            observation["reason"] = f"Population {population} cannot exercise the required reviewer capacity {budget}."
            return observation
    if field in {"ap", "recall_at_review_budget", "minimum_ap_delta"} and row.get("prevalence") == 0:
        observation["reason"] = "No positive labels for this task; AP, recall and AP comparison are not evaluable."
        return observation
    if field == "minimum_ap_delta" and metric_observation(report, task, "ap", budget, path)["status"] != "OBSERVED":
        observation["reason"] = "Candidate AP is not measurable; an AP-regression comparison cannot be verified."
        return observation
    value = report.get("comparison", {}).get(task, {}).get("ap_delta") if field == "minimum_ap_delta" else row.get(field)
    if value is None:
        observation["reason"] = f"Required metric {field} is missing/null."
        return observation
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        observation["reason"] = f"Required metric {field} is not a finite numeric measurement."
        return observation
    if field == "benign_false_positives":
        valid = isinstance(value, int) and value >= 0
    else:
        valid = (-1 if field == "minimum_ap_delta" else 0) <= value <= 1
    if not valid:
        observation["reason"] = f"Required metric {field} is outside its valid range."
        return observation
    observation.update(status="OBSERVED", value=value)
    return observation


def aggregate(paths, budget):
    if not paths:
        raise ValueError("at least one evaluation report is required")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        raise ValueError("review budget must be a nonnegative integer")
    reports = [json.loads(path.read_text()) for path in paths]
    releases = {row["model"]["release_id"] for row in reports}
    if len(releases) != 1:
        raise ValueError("worst-case summary must compare one frozen candidate release")
    if any(row.get("review_budget") is not None and row["review_budget"] != budget
           for report in reports for row in report.get("results", {}).values()):
        raise ValueError("review budgets differ; cannot silently compare different analyst capacities")
    worst, gates, details, coverage = {}, {}, {}, {}
    fields = ("ap", "p_at_100", "p_at_review_budget", "recall_at_review_budget", "benign_false_positives", "minimum_ap_delta")
    thresholds = {"ap_preferred": ("ap", .85), "ap_stretch": ("ap", .90),
        "p100": ("p_at_100", .90), "no_ap_regression": ("minimum_ap_delta", 0)}
    for task in TASKS:
        worst[task], gates[task], details[task], coverage[task] = {}, {}, {}, {}
        for field in fields:
            observations = [metric_observation(report, task, field, budget, path)
                for path, report in zip(paths, reports, strict=True)]
            values = [item["value"] for item in observations if item["status"] == "OBSERVED"]
            incomplete = any(item["status"] == "NOT EVALUABLE" for item in observations)
            status = "INCOMPLETE" if incomplete else "COMPLETE" if values else "NOT APPLICABLE"
            coverage[task][field] = {"status": status, "datasets": observations}
            output_field = "max_benign_false_positives" if field == "benign_false_positives" else field
            reducer = max if field == "benign_false_positives" else min
            # A partial minimum/maximum is not the worst dataset for the matrix.
            worst[task][output_field] = reducer(values) if status == "COMPLETE" else None
        for name, (field, threshold) in thresholds.items():
            metric = coverage[task][field]
            value = worst[task][field]
            if metric["status"] == "INCOMPLETE":
                status = "NOT EVALUABLE"
            elif metric["status"] == "NOT APPLICABLE":
                status = "NOT APPLICABLE"
            else:
                status = "PASS" if value >= threshold else "FAIL"
            gates[task][name] = status == "PASS"
            details[task][name] = {"status": status, "metric": field, "value": value, "threshold": threshold,
                "reasons": [item for item in metric["datasets"] if item["status"] != "OBSERVED"]}
    incomplete = any(metric["status"] == "INCOMPLETE" for task in coverage.values() for metric in task.values())
    queue_coverage = []
    if any("queue_comparison" in report for report in reports):
        for report in reports:
            queue = report.get("queue_comparison", {})
            queue_coverage.append({"dataset": report["dataset"], "status": queue.get("status", "NOT EVALUABLE"),
                "reasons": queue.get("reasons", ["Product queue comparison is missing."] if not queue else []),
                "eligible_transactions": queue.get("eligible_transactions"), "labelled_transactions": queue.get("labelled_transactions")})
        incomplete = incomplete or any(row["status"] != "EVALUATED" for row in queue_coverage)
    return {"schema": "quality-matrix-v2", "status": "INCOMPLETE" if incomplete else "EVALUATED_NOT_PROMOTED",
        "quality_gates_passed": not incomplete and all(passed for task in gates.values() for passed in task.values()),
        "procedure": "frozen-transfer per-dataset summaries; AP is not accuracy; no automatic universal/generalization certification",
        "model": {"release_id": next(iter(releases))}, "results": reports, "worst_case": worst, "gates": gates,
        "gate_details": details, "metric_coverage": coverage, "queue_coverage": queue_coverage,
        "matrix_coverage": {"datasets": [row["dataset"] for row in reports], "review_budget": budget,
            "limitations": "require independent authored positives, varied prevalence, unseen families and representative labels; stress copies are correlated; benign FP/calibration acceptability is an owner judgement"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fixture = sub.add_parser("fixture")
    fixture.add_argument("--output", type=Path, required=True)
    fixture.add_argument("--count", type=int, default=64)
    fixture.add_argument("--seed", type=int, required=True)
    fixture.add_argument("--role", required=True)
    variant = sub.add_parser("stress")
    variant.add_argument("--source", type=Path, required=True)
    variant.add_argument("--output", type=Path, required=True)
    variant.add_argument("--protocol", type=Path, required=True)
    variant.add_argument("--variant", choices=VARIANTS, required=True)
    variant.add_argument("--seed", type=int, required=True)
    summary = sub.add_parser("aggregate", description="Save a fail-closed quality summary: exit 0 for all threshold gates passing, 1 for failed/unexercised gates, 2 for incomplete metrics. No automatic promotion.")
    summary.add_argument("--result", type=Path, action="append", required=True)
    summary.add_argument("--output", type=Path, required=True)
    summary.add_argument("--review-budget", type=int, default=100)
    smoke = sub.add_parser("smoke", help="Fixed 64-row wiring populations, NOT model selection or quality acceptance")
    smoke.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output must be fresh; no holdout/report is overwritten")
    if args.command == "smoke":
        from scripts import candidate_lifecycle as lifecycle
        args.output.mkdir(parents=True, exist_ok=False)
        protocol, final = args.output / "protocol.json", args.output / "fresh-final"
        lifecycle.main(["register", "--protocol", str(protocol), "--final", str(final)])
        inputs = []
        for seed, role in enumerate(("training", "calibration", "validation"), 1):
            directory = args.output / role
            independent_fixture(directory, 64, seed, role)
            inputs.extend(["--" + role, str(directory), "--" + role + "-truth", str(directory / "ground_truth.json")])
        comparison = args.output / "comparison"
        lifecycle.main(["compare", "--protocol", str(protocol), *inputs, "--output", str(comparison), "--exclude-family", "independent_equal_outputs"])
        artifact = comparison / "hist"
        digest = sha(artifact / "manifest.json")
        independent_fixture(final, 64, 4, "final")
        lifecycle.main(["freeze", "--protocol", str(protocol), "--artifact", str(artifact), "--manifest-sha256", digest,
            "--validation-report", str(comparison / "comparison.json"), "--reason", "Fixed HGB for tiny command wiring only; NOT selected for quality/promotion"])
        lifecycle.main(["evaluate", "--protocol", str(protocol), "--artifact", str(artifact), "--manifest-sha256", digest,
            "--dataset", str(final), "--truth", str(final / "ground_truth.json"), "--output", str(args.output / "transfer.json"), "--review-budget", "10"])
        (args.output / "summary.json").write_text(json.dumps({"status": "PASS_WIRING_ONLY", "scope": "4 independently authored 64-row populations; NOT 3M or AP/generalization acceptance; no production promotion",
            "models": json.loads((comparison / "comparison.json").read_text())["models"],
            "results": json.loads((args.output / "transfer.json").read_text()),
            "reproduction": ["uv run python -m scripts.quality_matrix smoke --output var/NEW_64_ROW_WIRING_CHECK"]}, indent=2))
    elif args.command == "fixture":
        independent_fixture(args.output, args.count, args.seed, args.role)
    elif args.command == "stress":
        if str(args.output.resolve()) not in json.loads(args.protocol.read_text())["reserved_finals"]:
            parser.error("stress final must be reserved before selection")
        stress(args.source, args.output, args.variant, args.seed)
    else:
        report = aggregate(args.result, args.review_budget)
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2)
        if report["status"] == "INCOMPLETE":
            return 2
        return 0 if report["quality_gates_passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
