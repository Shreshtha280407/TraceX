"""Retire explicitly named verified review-generator inputs, preserving all manifests/truth."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def verified_ingestion_only(dataset, manifest, benchmark):
    """Retire the last generated input only after its full benchmark passed."""
    previous = json.loads((dataset / "disposable-input-archive.json").read_text())
    canonical = {"transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson"}
    if not previous.get("delete_requested") or {row["name"] for row in previous["artifacts"]} != canonical:
        raise ValueError("Expected a preserved approved canonical-input retirement")
    if any((dataset / name).exists() or (dataset / name).is_symlink() for name in canonical):
        raise ValueError("Canonical inputs have not been retired")
    if any(row["sha256"] != manifest["files"][row["name"]]["sha256"] for row in previous["artifacts"]):
        raise ValueError("Canonical retirement does not match this manifest")
    if benchmark.get("status") != "pass" or benchmark.get("acceptance_errors") or benchmark.get("source_sha256") != manifest["files"]["ingestion_rows.ndjson"]["sha256"]:
        raise ValueError("Matching full passing benchmark required before retiring its last input")
    return ["ingestion_rows.ndjson"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--keep-ingestion", action="store_true")
    choice.add_argument("--only-ingestion", action="store_true", help="After verified canonical retirement and a matching full passing benchmark")
    parser.add_argument("--completed-benchmark", type=Path)
    parser.add_argument("--delete", action="store_true", help="User-authorized disposable data only; unrecoverable")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "datasets"
    if args.dataset.is_symlink():
        raise ValueError("Dataset directory symlinks are not allowed")
    dataset = args.dataset.resolve(strict=True)
    manifest = json.loads((dataset / "dataset_manifest.json").read_text())
    if dataset.parent != root or not dataset.name.startswith("review_") or not manifest["seed"].startswith("tracex-review-"):
        raise ValueError("Only this task's explicitly named synthetic review corpora are eligible")
    benchmark = None
    if args.only_ingestion:
        if args.completed_benchmark is None:
            parser.error("--only-ingestion requires --completed-benchmark")
        benchmark = json.loads(args.completed_benchmark.read_text())
        names = verified_ingestion_only(dataset, manifest, benchmark)
    else:
        names = ["transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson"]
        if not args.keep_ingestion:
            names.append("ingestion_rows.ndjson")
    report = {"dataset": str(dataset), "seed": manifest["seed"], "artifacts": [],
              "kept": "dataset manifest, evaluation-only truth, final reservations/evaluation markers and reports",
              "delete_requested": args.delete}
    if benchmark is not None:
        report["completed_benchmark"] = str(args.completed_benchmark)
        report["completed_benchmark_sha256"] = hashlib.sha256(args.completed_benchmark.read_bytes()).hexdigest()
    for name in names:
        target = dataset / name
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"Expected regular generated file: {name}")
        with target.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        if checksum != manifest["files"][name]["sha256"] or target.stat().st_size != manifest["files"][name]["bytes"]:
            raise ValueError(f"Generated input integrity mismatch: {name}; nothing deleted")
        report["artifacts"].append({"name": name, "sha256": checksum, "bytes": target.stat().st_size})
    report["logical_bytes"] = sum(row["bytes"] for row in report["artifacts"])
    archive_name = "disposable-ingestion-archive.json" if args.only_ingestion else "disposable-input-archive.json"
    with (dataset / archive_name).open("x") as handle:
        json.dump(report, handle, indent=2)
    if args.delete:
        for name in names:
            (dataset / name).unlink()
    print(json.dumps({"dataset": str(dataset), "bytes": report["logical_bytes"], "deleted": args.delete}))


if __name__ == "__main__":
    main()
