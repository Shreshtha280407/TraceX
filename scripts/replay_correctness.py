"""Space-conscious derived-stage correctness replay, NOT an upload/scale benchmark.

Copies only control metadata; hard-links immutable inputs/graph and recomputes
features, findings, ML and analytics in an isolated vault. Never changes the
reference control database or deletes its artifacts. Run canonical_parity.py
afterwards to compare the full semantic outputs.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from scripts.scale_benchmark import REPO, _rss_mb, _tree_rss_mb


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--memory-budget-mb", type=int, default=2600)
    parser.add_argument("--execution-mode", choices=("memory", "bounded"), default="bounded")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    reference, output = args.reference.resolve(), args.output.resolve()
    if not (REPO / "var/scale-run").resolve() in output.parents:
        parser.error("output must be a new directory under var/scale-run")
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "failed", "verification_type": "derived-stage replay; NOT an HTTP scale benchmark",
              "reference": str(reference), "workers": args.workers, "memory_budget_mb": args.memory_budget_mb,
              "execution_mode": args.execution_mode,
              "disk_initial": shutil.disk_usage(output)._asdict()}
    process = None
    started = time.monotonic()
    try:
        prior = json.loads((reference / "result.json").read_text())
        accepted = prior["rows_accepted"]
        if prior["status"] != "pass":
            raise ValueError("reference must be a successful frozen run")
        required_space = accepted * 12_000 + (512 << 20)
        report["scratch_admission_estimate_bytes"] = required_space
        if shutil.disk_usage(output).free < required_space:
            raise RuntimeError("insufficient scratch disk for safe correctness replay")
        env = {**os.environ, "TRACEX_DATABASE_URL": f"sqlite:///{output / 'control.db'}",
               "TRACEX_EVIDENCE_ROOT": str(output / "evidence"), "TRACEX_WORKERS": str(args.workers),
               "TRACEX_MEMORY_BUDGET_MB": str(args.memory_budget_mb), "TRACEX_EXECUTION_MODE": args.execution_mode,
               "TRACEX_ML_FINDINGS": "1", "PYTHONFAULTHANDLER": "1"}
        subprocess.run([sys.executable, "-c", "from app.db import init_database; init_database()"],
                       cwd=REPO, env=env, check=True, timeout=30, capture_output=True, text=True)
        db = sqlite3.connect(output / "control.db", uri=True)
        try:
            db.execute("ATTACH DATABASE ? AS reference", [f"file:{reference / 'control.db'}?mode=ro"])
            for table in ("users", "cases", "case_members", "evidence_sources", "import_jobs", "snapshots",
                          "fragment_receipts", "import_checkpoints", "graph_snapshots", "address_activity"):
                source_cols = {r[1] for r in db.execute(f"PRAGMA reference.table_info({table})")}
                columns = [r[1] for r in db.execute(f"PRAGMA main.table_info({table})") if r[1] in source_cols]
                if columns:
                    names = ",".join(f'"{name}"' for name in columns)
                    db.execute(f"INSERT INTO main.{table} ({names}) SELECT {names} FROM reference.{table}")
            db.execute("INSERT INTO analysis_stages SELECT * FROM reference.analysis_stages "
                       "WHERE name IN ('source_verification', 'ingesting')")
            db.execute("UPDATE import_jobs SET state='queued', stage='queued', completed_at=NULL, "
                       "error_code=NULL, error_detail=NULL, lease_owner=NULL, lease_expires_at=NULL")
            db.commit()
            paths = set()
            for table in ("evidence_sources", "fragment_receipts", "graph_snapshots"):
                paths.update(row[0] for row in db.execute(f"SELECT storage_relative_path FROM {table}"))
            for relative in sorted(paths):
                source = (reference / "evidence" / relative).resolve(strict=True)
                target = (output / "evidence" / relative).resolve()
                if not (reference / "evidence").resolve() in source.parents or not (output / "evidence").resolve() in target.parents:
                    raise ValueError("immutable artifact path escaped vault")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
            report["hardlinked_immutable_artifacts"] = len(paths)
        finally:
            db.close()
        peak, disk_min = 0, shutil.disk_usage(output).free
        with (output / "worker.log").open("x") as log:
            process = subprocess.Popen([sys.executable, "-m", "workers.runner", "--once", "--worker-id", "correctness-replay",
                                        "--profile-output", str(output / "worker.prof")], cwd=REPO, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            with (output / "resources.ndjson").open("x") as samples:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    values = {"elapsed_seconds": elapsed, "worker_rss_mb": _rss_mb(process.pid),
                              "worker_tree_rss_mb": _tree_rss_mb(process.pid),
                              "disk_free_bytes": shutil.disk_usage(output).free}
                    peak = max(peak, values["worker_tree_rss_mb"])
                    disk_min = min(disk_min, values["disk_free_bytes"])
                    samples.write(json.dumps(values) + "\n")
                    samples.flush()
                    if elapsed > args.timeout:
                        raise TimeoutError("derived correctness replay timeout")
                    time.sleep(1)
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from app.db import make_engine
        from app.jobs.analysis import REQUIRED_STAGES, SUCCESS, analysis_view
        from app.models import ImportJob

        engine = make_engine(f"sqlite:///{output / 'control.db'}")
        with Session(engine) as session:
            job = session.scalar(select(ImportJob))
            view = analysis_view(session, job)
            stages = {row["name"]: row for row in view["stages"]}
            errors = [name for name in REQUIRED_STAGES if stages.get(name, {}).get("status") not in SUCCESS]
            report.update({"analysis": view, "rows_accepted": job.rows_accepted, "job_state": job.state,
                           "error": job.error_detail, "mandatory_stage_errors": errors,
                           "worker_tree_peak_rss_mb": peak, "disk_min_free_bytes": disk_min,
                           "worker_exit_code": process.returncode})
            report["status"] = "pass" if process.returncode == 0 and job.state == "completed" and not errors else "failed"
        engine.dispose()
    except Exception as error:  # noqa: BLE001 - every verification failure is retained
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process and process.poll() is None:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        report.update({"elapsed_seconds": time.monotonic() - started,
                       "disk_final": shutil.disk_usage(output)._asdict()})
        with (output / "result.json").open("x") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(json.dumps({"status": report["status"], "result": str(output / "result.json"),
                          "seconds": report["elapsed_seconds"], "error": report.get("error")}))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
