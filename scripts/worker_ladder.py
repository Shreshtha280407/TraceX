"""Matched serial 300K HTTP worker ladder; preserve failed and successful evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--budget", type=int, default=5120)
    parser.add_argument("--min-transactions", type=int, required=True)
    args = parser.parse_args()
    output = REPO / "experiments/runs" / f"{args.prefix}.json"
    if output.exists():
        raise FileExistsError(output)
    from scripts.canonical_parity import fingerprints

    reports, reference = [], None
    try:
        for workers in (1, 2, 4, 6, 8):
            name = f"{args.prefix}-w{workers}"
            command = [sys.executable, "scripts/scale_benchmark.py", str(args.source), "--upload-filename", "ingestion_rows.ndjson",
                       "--name", name, "--port", "8765", "--workers", str(workers), "--memory-budget-mb", str(args.budget),
                       "--execution-mode", "bounded", "--min-transactions", str(args.min_transactions), "--keep"]
            completed = subprocess.run(command, cwd=REPO, check=False)
            run = REPO / "var/scale-run" / name
            result = json.loads((run / "result.json").read_text())
            report = {"workers": workers, "run": str(run), "exit_code": completed.returncode,
                      "status": result["status"], "seconds": result.get("total_seconds"),
                      "tree_peak_rss_mb": result.get("worker_tree_peak_rss_mb"), "errors": result["acceptance_errors"],
                      "diff_sha256": result["environment"]["diff_sha256"], "locks": result["environment"]["locks"]}
            report["runtime_source_sha256"] = result["environment"]["runtime_source_sha256"]
            if result["status"] == "pass":
                digest = fingerprints(run)
                with (run / "canonical-fingerprints.json").open("x") as handle:
                    json.dump(digest, handle, indent=2)
                if reference is None:
                    reference = digest
                report["full_canonical_parity"] = digest == reference
                # Ranking-only signature independent of random snapshot IDs;
                # complete published-field parity is tested by isolated replay.
                import sqlite3

                db = sqlite3.connect(f"file:{run / 'control.db'}?mode=ro", uri=True)
                ranked = db.execute("SELECT entity_ref,raw_score,rank FROM findings WHERE rule_version='anomaly-stack-v2' ORDER BY entity_ref").fetchall()
                report["ml_ranking_sha256"] = hashlib.sha256(json.dumps(ranked).encode()).hexdigest()
                db.close()
                print(json.dumps({"worker_result": report}), flush=True)
            reports.append(report)
    finally:
        safe = [r for r in reports if r["status"] == "pass" and r.get("full_canonical_parity")]
        fastest = min((r["seconds"] for r in safe), default=None)
        selected = min((r["workers"] for r in safe if r["seconds"] <= fastest * 1.05), default=None)
        with output.open("x") as handle:
            json.dump({"protocol": "serial same source, all mandatory stages, fixed 5120 MiB budget, no profiler",
                       "reports": reports, "selected_smallest_within_5pct": selected,
                       "fastest_safe_seconds": fastest, "limitation": "Shared workstation; external background load is not controlled"}, handle, indent=2)
    return int(len(reports) != 5 or any(r["status"] != "pass" or not r.get("full_canonical_parity") for r in reports))


if __name__ == "__main__":
    raise SystemExit(main())
