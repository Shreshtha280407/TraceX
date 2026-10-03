"""Label-free, registered stress preparation and compact per-task worst-case evaluation.

No training/selection occurs here. Variants retain source labels: their changed
data coverage and task-label applicability must be reviewed, never assumed.
The independent fixture is capped at 1,000 transactions for wiring checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
    (output / "ground_truth.json").write_bytes((source / "ground_truth.json").read_bytes())
    inventory(output, {"source_manifest_sha256": sha(source / "quality_manifest.json") if (source / "quality_manifest.json").exists() else None,
        "source_truth_sha256": sha(source / "ground_truth.json"), "variant": variant, "seed": seed,
        "limitation": "source labels retained; review task-label validity after timing/value/reuse perturbation; not independent populations"})


def aggregate(paths, budget):
    reports = [json.loads(path.read_text()) for path in paths]
    releases = {row["model"]["release_id"] for row in reports}
    if len(releases) != 1:
        raise ValueError("worst-case summary must compare one frozen candidate release")
    if any(row.get("review_budget") != budget for report in reports for row in report["results"].values()):
        raise ValueError("review budgets differ; cannot silently compare different analyst capacities")
    worst, gates = {}, {}
    for task in TASKS:
        rows = [report["results"][task] for report in reports]
        worst[task] = {field: min((row[field] for row in rows if row.get(field) is not None), default=None) for field in ("ap", "p_at_100", "p_at_review_budget", "recall_at_review_budget")}
        worst[task]["max_benign_false_positives"] = max((row.get("benign_false_positives") or 0 for row in rows), default=0)
        delta = [report.get("comparison", {}).get(task, {}).get("ap_delta") for report in reports]
        worst[task]["minimum_ap_delta"] = min((value for value in delta if value is not None), default=None)
        gates[task] = {"ap_preferred": worst[task]["ap"] is not None and worst[task]["ap"] >= .85,
            "ap_stretch": worst[task]["ap"] is not None and worst[task]["ap"] >= .90,
            "p100": "NOT APPLICABLE" if worst[task]["p_at_100"] is None else worst[task]["p_at_100"] >= .90,
            "no_ap_regression": worst[task]["minimum_ap_delta"] is not None and worst[task]["minimum_ap_delta"] >= 0}
    return {"status": "EVALUATED_NOT_PROMOTED", "procedure": "frozen-transfer per-dataset summaries; AP is not accuracy; no automatic universal/generalization certification",
        "model": {"release_id": next(iter(releases))}, "results": reports, "worst_case": worst, "gates": gates,
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
    summary = sub.add_parser("aggregate")
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
        lifecycle.main(["freeze", "--protocol", str(protocol), "--artifact", str(artifact), "--manifest-sha256", digest,
            "--validation-report", str(comparison / "comparison.json"), "--reason", "Fixed HGB for tiny command wiring only; NOT selected for quality/promotion"])
        independent_fixture(final, 64, 4, "final")
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
        with args.output.open("x") as stream:
            json.dump(aggregate(args.result, args.review_budget), stream, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
