"""Save a small reviewable index of local verification artifacts; no secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    paths = set()
    for pattern in ("experiments/runs/*.json", "experiments/*counts*.json", "experiments/*protocol*.json",
                    "docs/implementation_*2026-10-03.md", "experiments/model_decision_review_20261003.md",
                    "docs/scale.md", "docs/phase7.md",
                    "var/scale-run/*/result.json", "var/scale-run/*/disposable*.json",
                    "var/scale-run/*/resources.ndjson", "var/scale-run/*/scratch-usage.ndjson",
                    "var/scale-run/*/worker.prof", "var/scale-run/*/*.log",
                    "var/appliance-runs/*/result.json", "var/browser-runs/*/result.json",
                    "var/appliance-runs/*/resources.ndjson", "var/appliance-runs/*/progress.ndjson",
                    "var/appliance-runs/*/*.log",
                    "var/offline-install-*/installation_result.json", "var/offline-install-*/manifest.json",
                    "var/offline-install-*/SHA256SUMS", "var/verification/*.xml",
                    "datasets/review_*/dataset_manifest.json", "datasets/review_*/disposable*.json",
                    "datasets/phase5a_100k/fixture_manifest.json", "datasets/review_*/evaluation_truth.json",
                    "datasets/review_*/deduplicated-source-*.json",
                    "datasets/review_*/.final_reservation.json", "datasets/review_*/.reserved_final_evaluation.json",
                    "dist/tracex-offline-*/manifest.json", "dist/tracex-offline-*/SHA256SUMS"):
        paths.update(REPO.glob(pattern))
    artifacts = []
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(REPO) or not path.is_file():
            raise ValueError("Unsafe verification artifact path")
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        item = {"path": str(path.relative_to(REPO)), "bytes": path.stat().st_size, "sha256": checksum}
        if path.suffix == ".json":
            report = json.loads(path.read_text())
            if isinstance(report, dict):
                for key in ("status", "pass", "total_seconds", "rows_accepted", "worker_tree_peak_rss_mb"):
                    if key in report:
                        item[key] = report[key]
                check_rows = report.get("results", report.get("checks"))
                if isinstance(check_rows, list):
                    checks = [r for r in check_rows if isinstance(r, dict) and "ok" in r]
                    item["checks"] = len(checks)
                    item["failed_checks"] = sum(not r["ok"] for r in checks)
        artifacts.append(item)
    report = {"schema": "tracex-implementation-review-inventory-v1",
              "created_at": datetime.now(UTC).isoformat(),
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
              "no_commit_or_push": True,
              "scope": "artifact integrity index, NOT an assertion that every acceptance gate passes",
              "raw_reports": "retained locally; ignored large reports are not automatically staged for commit",
              "artifacts": artifacts}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"output": str(args.output), "artifacts": len(artifacts)}))


if __name__ == "__main__":
    main()
