"""End-to-end scale benchmark in the real deployment shape.

    uv run --extra ml python scripts/scale_benchmark.py datasets/scaled/rows_3m.ndjson

Starts the API (uvicorn) and one worker as separate processes against a fresh
SQLite database and evidence vault under var/scale-run/<name>/, uploads the
file with a streamed multipart POST (as a browser or curl would), and polls the
job: wall time per pipeline stage, the worker's peak resident memory, and the
final counts (findings, entity clusters, network observations) are written as
JSON next to the run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _rss_mb(pid: int) -> float | None:
    from app.telemetry import process_rss_bytes
    value = process_rss_bytes(pid)
    return value / (1 << 20) if value is not None else None


def _tree_rss_mb(pid: int) -> float | None:
    from app.telemetry import process_rss_bytes
    value = process_rss_bytes(pid, tree=True)
    return value / (1 << 20) if value is not None else None


def observed_max(*values):
    available = [value for value in values if value is not None]
    return max(available) if available else None


def _get(base: str, path: str, headers: dict, attempts: int = 3, timeout: float = 10) -> dict:
    for attempt in range(attempts):
        request = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read())
            if not isinstance(value, dict):
                raise TypeError(f"invalid response schema from {path}")
            return value
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
            time.sleep(.2)
    raise RuntimeError("unreachable")


def _sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _environment(source, work):
    from app.telemetry import host_metrics
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=REPO)

    versions = {}
    for name in ("duckdb", "pyarrow", "numpy", "scikit-learn", "scipy", "SQLAlchemy", "psycopg"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    import sqlite3

    return {"commit": git("rev-parse", "HEAD").decode().strip(),
            "git_status": git("status", "--porcelain").decode(),
            "diff_sha256": hashlib.sha256(git("diff", "HEAD", "--binary")).hexdigest(),
            "untracked_sha256": {name: _sha(REPO / name) for name in
                                  git("ls-files", "--others", "--exclude-standard").decode().splitlines()
                                  if (REPO / name).is_file()},
            "locks": {name: _sha(REPO / name) for name in ("uv.lock", "frontend/package-lock.json")},
            "runtime_source_sha256": {str(p.relative_to(REPO)): _sha(p) for folder in ("app", "workers")
                                      for p in sorted((REPO / folder).rglob("*.py"))},
            "python": sys.version, "versions": versions, "sqlite_version": sqlite3.sqlite_version,
            "uv_version": subprocess.check_output(["uv", "--version"], text=True).strip(),
            "host": platform.platform(), "cpu_count": os.cpu_count(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "child_thread_caps": {name: 1 for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                                                       "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")},
            "resources": host_metrics(), "source_sha256": _sha(source),
            "manifests": {p.name: {"sha256": _sha(p), "content": json.loads(p.read_text())}
                          for p in source.parent.glob("*manifest.json")},
            "disk": shutil.disk_usage(work)._asdict(),
            "cgroup": {name: Path("/sys/fs/cgroup", name).read_text().strip() for name in
                       ("memory.max", "memory.peak", "cpu.max") if Path("/sys/fs/cgroup", name).is_file()},
            "settings": {k: v for k, v in os.environ.items() if k in {
                "TRACEX_WORKERS", "TRACEX_MEMORY_BUDGET_MB", "TRACEX_EXECUTION_MODE", "TRACEX_GEOIP_DIR",
                "TRACEX_ML_FINDINGS", "TRACEX_ML_REVIEW_BUDGET", "TRACEX_INGESTION_BATCH_RECORDS"}}}


def acceptance_errors(job, inspection, *, required_stages, expected_counts=None, require_geoip=True,
                      min_transactions=None, max_seconds=None, seconds=0, expected_release="anomaly-stack-v2"):
    errors = []
    if job.get("state") != "completed":
        errors.append(f"job state {job.get('state')}: {job.get('error_detail')}")
    stages = {s["name"]: s for s in (job.get("analysis") or {}).get("stages", [])}
    for name in required_stages:
        if stages.get(name, {}).get("status") not in {"complete", "written", "no_rows_flagged"}:
            errors.append(f"mandatory stage {name}: {stages.get(name, {}).get('status', 'missing')}")
    if "ml_scoring" in required_stages and stages.get("ml_scoring", {}).get("details", {}).get("release_id") != expected_release:
        errors.append("model release missing or mismatched")
    if require_geoip and not inspection.get("geoip", {}).get("installed"):
        errors.append("offline Geo-IP database missing")
    analytics = inspection.get("analytics")
    if "analytics" in required_stages and not analytics:
        errors.append("analytics snapshot missing")
    if analytics and analytics.get("wallet_count", 0) > 1 and not analytics.get("embedded_wallets"):
        errors.append("mandatory embeddings missing")
    counts = inspection.get("canonical_counts", {})
    for kind, expected in (expected_counts or {}).items():
        if counts.get(kind) != expected:
            errors.append(f"canonical {kind}: expected {expected}, got {counts.get(kind)}")
    if min_transactions and counts.get("transactions", 0) < min_transactions:
        errors.append(f"canonical transactions below {min_transactions}")
    if job.get("rows_seen") != job.get("rows_accepted", 0) + job.get("rows_quarantined", 0):
        errors.append("source row reconciliation failed")
    if max_seconds is not None and seconds >= max_seconds:
        errors.append(f"elapsed {seconds:.3f}s must be less than target {max_seconds}s")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--upload-filename", help="Original logical filename for immutable vault sources named 'original'")
    parser.add_argument("--name", default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--keep", action="store_true", help="keep the run's database and evidence vault")
    parser.add_argument("--database-url", help="dedicated empty PostgreSQL database (never reset)")
    parser.add_argument("--resume-from", type=Path, help="retry a failed SQLite run from its immutable receipts; new logs/result")
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument("--poll-timeout", type=float, default=10)
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--sample-seconds", type=float, default=1)
    parser.add_argument("--required-stages", default="source_verification,ingesting,graph_building,findings,ml_scoring,analytics,investigation_grouping")
    parser.add_argument("--allow-missing-geoip", action="store_true", help="diagnostic runs; excludes the Geo-IP gate")
    parser.add_argument("--expected-counts", type=Path, help="JSON object containing exact canonical counts")
    parser.add_argument("--min-transactions", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--profile-worker", action="store_true")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--memory-budget-mb", type=int)
    parser.add_argument("--execution-mode", choices=("memory", "bounded"))
    args = parser.parse_args(argv)
    if args.upload_filename and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.upload_filename):
        parser.error("upload filename must be a safe basename")
    if any(v <= 0 for v in (args.timeout, args.poll_timeout, args.startup_timeout, args.sample_seconds)):
        parser.error("timeouts and sampling interval must be positive")
    name = args.name or datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        parser.error("run name must be a safe path component")
    work = REPO / "var" / "scale-run" / name
    work.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "TRACEX_DATABASE_URL": args.database_url or f"sqlite:///{work / 'control.db'}",
        "TRACEX_EVIDENCE_ROOT": str(work / "evidence"),
        "TRACEX_ALLOW_DEV_ACTOR_HEADER": "1",
        "TRACEX_MAX_UPLOAD_BYTES": str(64 * 1024 ** 3),
        "TRACEX_LEASE_SECONDS": "600",
    }
    previous = None
    for value, key in ((args.workers, "TRACEX_WORKERS"), (args.memory_budget_mb, "TRACEX_MEMORY_BUDGET_MB"),
                       (args.execution_mode, "TRACEX_EXECUTION_MODE")):
        if value is not None:
            env[key] = str(value)
    python, processes, logs = sys.executable, [], []
    result = {"run_id": name, "source": str(args.source), "status": "failed", "acceptance_errors": []}
    result["configuration"] = {"workers": env.get("TRACEX_WORKERS"), "memory_budget_mb": env.get("TRACEX_MEMORY_BUDGET_MB"),
                               "execution_mode": env.get("TRACEX_EXECUTION_MODE"),
                               "required_stages": args.required_stages.split(","), "require_geoip": not args.allow_missing_geoip}
    job, peak, tree_peak, api_peak = {}, None, None, None
    started = time.monotonic()
    deadline = started + args.timeout
    sampler = None
    sampling_stop = threading.Event()
    samples = {"worker_peak_rss_mb": None, "worker_tree_peak_rss_mb": None, "api_peak_rss_mb": None}
    base = f"http://127.0.0.1:{args.port}"
    headers = {"X-TraceX-Actor": "scale-benchmark"}
    try:
        if args.resume_from:
            if args.database_url:
                raise ValueError("resume-from currently supports SQLite runs only")
            previous = json.loads((args.resume_from / "result.json").read_text())
            if previous.get("job", {}).get("state") != "failed":
                raise ValueError("resume requires a terminal failed job, not an active/abandoned lease")
            env["TRACEX_DATABASE_URL"] = f"sqlite:///{args.resume_from.resolve() / 'control.db'}"
            env["TRACEX_EVIDENCE_ROOT"] = str(args.resume_from.resolve() / "evidence")
        result["environment"] = _environment(args.source.resolve(strict=True), work)
        result["database_type"] = "postgresql" if args.database_url else "sqlite"
        if args.database_url:
            from sqlalchemy import create_engine, inspect, text

            database = create_engine(args.database_url)
            try:
                with database.connect() as connection:
                    result["postgres_version"] = connection.execute(text("SELECT version()")).scalar()
                    if inspect(connection).has_table("cases") and connection.execute(text("SELECT count(*) FROM cases")).scalar():
                        raise ValueError("benchmark requires a dedicated empty database")
            finally:
                database.dispose()
        init_log = (work / "init.log").open("w")
        logs.append(init_log)
        subprocess.run([python, "-c", "from app.db import init_database; init_database()"], env=env, check=True,
                       cwd=REPO, timeout=args.startup_timeout, stdout=init_log, stderr=subprocess.STDOUT)
        api_log, worker_log = (work / "api.log").open("w"), (work / "worker.log").open("w")
        logs.extend((api_log, worker_log))
        api = subprocess.Popen([python, "-m", "uvicorn", "app.main:app", "--port", str(args.port), "--log-level", "warning"],
                               env=env, cwd=REPO, stdout=api_log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(api)
        worker_command = [python, "-m", "workers.runner", "--worker-id", f"scale-{name}"]
        if args.profile_worker:
            worker_command.extend(["--profile-output", str(work / "worker.prof")])
        worker = subprocess.Popen(worker_command, env=env, cwd=REPO,
                                  stdout=worker_log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(worker)
        startup_deadline = min(deadline, time.monotonic() + args.startup_timeout)
        while time.monotonic() < startup_deadline:
            if api.poll() is not None or worker.poll() is not None:
                raise RuntimeError("API or worker died during startup")
            try:
                _get(base, "/v1/healthz", headers, attempts=1, timeout=min(2, args.poll_timeout))
                break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(.2)
        else:
            raise TimeoutError("API startup timeout")
        request = urllib.request.Request(
            base + "/v1/cases", data=json.dumps({"name": f"scale {name}", "synthetic": True}).encode(),
            headers={**headers, "Content-Type": "application/json"}, method="POST",
        )
        if previous:
            case_id = previous["case_id"]
        else:
            with urllib.request.urlopen(request, timeout=args.poll_timeout) as response:
                case_id = json.loads(response.read())["case_id"]
        pipeline_started = time.monotonic()

        def sample():
            with (work / "resources.ndjson").open("w") as handle:
                while not sampling_stop.is_set():
                    values = {"worker_peak_rss_mb": _rss_mb(worker.pid), "worker_tree_peak_rss_mb": _tree_rss_mb(worker.pid),
                              "api_peak_rss_mb": _rss_mb(api.pid)}
                    for key, value in values.items():
                        samples[key] = observed_max(samples[key], value)
                    handle.write(json.dumps({"elapsed_seconds": time.monotonic() - pipeline_started,
                                             "observed_stage": job.get("stage"), "observed_attempt": job.get("attempt"),
                                             "disk_free_bytes": shutil.disk_usage(work).free, **values}) + "\n")
                    handle.flush()
                    sampling_stop.wait(args.sample_seconds)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        if previous:
            job_id = previous["job_id"]
            request = urllib.request.Request(base + f"/v1/cases/{case_id}/analysis/retry?job_id={job_id}",
                                             headers=headers, data=b"", method="POST")
            with urllib.request.urlopen(request, timeout=args.poll_timeout) as response:
                json.loads(response.read())
            upload_seconds = 0
            result["resumed_from"] = str(args.resume_from)
        else:
            upload = subprocess.run(
            ["curl", "--fail-with-body", "-sS", "--max-time", str(max(.01, deadline - time.monotonic())),
             "-X", "POST", f"{base}/v1/cases/{case_id}/imports", "-H", "X-TraceX-Actor: scale-benchmark",
             "-H", f"Idempotency-Key: scale-{name}", "-F",
             f"file=@{args.source}" + (f";filename={args.upload_filename}" if args.upload_filename else "")],
            capture_output=True, text=True, check=True, timeout=max(.01, deadline - time.monotonic()),
        )
            job_id = json.loads(upload.stdout)["job_id"]
            upload_seconds = time.monotonic() - pipeline_started
        stages: dict[str, float] = {}
        result.update({"case_id": case_id, "job_id": job_id, "upload_seconds": upload_seconds})
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("overall benchmark timeout")
            peak = observed_max(peak, _rss_mb(worker.pid))
            tree_peak = observed_max(tree_peak, _tree_rss_mb(worker.pid))
            api_peak = observed_max(api_peak, _rss_mb(api.pid))
            try:
                job = _get(base, f"/v1/jobs/{job_id}", headers, attempts=1,
                           timeout=min(args.poll_timeout, max(.01, deadline - time.monotonic())))
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if worker.poll() is not None or api.poll() is not None:
                    raise RuntimeError("API or worker died during transport failure")
                time.sleep(min(args.sample_seconds, max(.01, deadline - time.monotonic())))
                continue
            if not {"state", "stage", "rows_seen"}.issubset(job):
                raise ValueError("job response schema missing required fields")
            stages.setdefault(job["stage"], round(time.monotonic() - pipeline_started, 3))
            if job["state"] in {"completed", "failed"}:
                break
            if worker.poll() is not None or api.poll() is not None:
                raise RuntimeError(f"API or worker died: {api.poll()}, {worker.poll()}")
            time.sleep(args.sample_seconds)
        final_findings = _get(base, f"/v1/cases/{case_id}/findings?limit=1", headers,
            timeout=min(args.poll_timeout, max(.01, deadline - time.monotonic())))
        if time.monotonic() >= deadline or "findings" not in final_findings:
            raise TimeoutError("final findings retrieval did not finish within the deadline")
        total = round(time.monotonic() - pipeline_started, 3)
        result["final_findings_retrieval"] = {"total": final_findings.get("total"), "returned": len(final_findings["findings"])}
        result["clock_contract"] = "upload initiation to final findings retrieval; resumed runs are not fresh acceptance"
        result.update({
            "source": str(args.source), "source_bytes": args.source.stat().st_size,
            "state": job.get("state"), "error": job.get("error_detail"), "worker_exit_code": job.get("worker_exit_code"),
            "rows_seen": job.get("rows_seen"), "rows_accepted": job.get("rows_accepted"),
            "rows_quarantined": job.get("rows_quarantined"), "upload_seconds": round(upload_seconds, 1),
            "total_seconds": total, "stage_started_at_seconds": stages, "worker_peak_rss_mb": peak,
            "worker_tree_peak_rss_mb": tree_peak,
            "machine": _get(base, "/v1/healthz", headers).get("resources"),
        })
        inspection = _get(base, f"/v1/cases/{case_id}/analysis?job_id={job_id}", headers, timeout=args.poll_timeout)
        inspection["geoip"] = _get(base, "/v1/geoip/status", headers, timeout=args.poll_timeout)
        result["inspection"] = inspection
        result["acceptance_errors"] = acceptance_errors(job, inspection, required_stages=args.required_stages.split(","),
            expected_counts=json.loads(args.expected_counts.read_text()) if args.expected_counts else None,
            require_geoip=not args.allow_missing_geoip, min_transactions=args.min_transactions,
            max_seconds=args.max_seconds, seconds=total)
        result["status"] = "pass" if not result["acceptance_errors"] else "failed"
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - persist every failed run
        result["error"] = f"{type(error).__name__}: {error}"
        if isinstance(error, subprocess.CalledProcessError):
            result["subprocess_stdout"] = (error.stdout or "")[-4096:]
            result["subprocess_stderr"] = (error.stderr or "")[-4096:]
        result["acceptance_errors"].append(result["error"])
    finally:
        sampling_stop.set()
        if sampler:
            sampler.join(timeout=5)
        peak = observed_max(peak, samples["worker_peak_rss_mb"])
        tree_peak = observed_max(tree_peak, samples["worker_tree_peak_rss_mb"])
        api_peak = observed_max(api_peak, samples["api_peak_rss_mb"])
        result.update({"job": job, "elapsed_seconds": time.monotonic() - started,
                       "worker_peak_rss_mb": peak, "worker_tree_peak_rss_mb": tree_peak,
                       "api_peak_rss_mb": api_peak, "sampling_interval_seconds": args.sample_seconds,
                       "memory_measurement": "sampled RSS can miss peaks; summed RSS can double-count shared pages",
                       "disk_final": shutil.disk_usage(work)._asdict()})
        (work / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        for log in logs:
            log.flush()
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for log in logs:
            log.close()
        print(json.dumps({"result": str(work / "result.json"), "status": result["status"],
                          "seconds": result.get("total_seconds"), "rows_accepted": result.get("rows_accepted"),
                          "worker_tree_peak_rss_mb": tree_peak, "acceptance_errors": result["acceptance_errors"]}, indent=2))
    return int(result["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
