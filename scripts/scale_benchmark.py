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
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _rss_mb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return 0
    return 0


def _get(base: str, path: str, headers: dict, attempts: int = 10) -> dict:
    for attempt in range(attempts):
        request = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read())
        except OSError:  # includes HTTPError; a poll must not abort a long import
            if attempt == attempts - 1:
                raise
            time.sleep(3)
    raise RuntimeError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--name", default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--keep", action="store_true", help="keep the run's database and evidence vault")
    args = parser.parse_args()
    name = args.name or args.source.stem
    work = REPO / "var" / "scale-run" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    env = {
        **os.environ,
        "TRACEX_DATABASE_URL": f"sqlite:///{work / 'control.db'}",
        "TRACEX_EVIDENCE_ROOT": str(work / "evidence"),
        "TRACEX_ALLOW_DEV_ACTOR_HEADER": "1",
        "TRACEX_MAX_UPLOAD_BYTES": str(64 * 1024 ** 3),
        "TRACEX_LEASE_SECONDS": "600",
    }
    python = sys.executable
    subprocess.run([python, "-c", "from app.db import init_database; init_database()"], env=env, check=True, cwd=REPO)
    api = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--port", str(args.port), "--log-level", "warning"],
        env=env, cwd=REPO,
    )
    worker_log = (work / "worker.log").open("w")
    worker = subprocess.Popen(
        [python, "-m", "workers.runner", "--worker-id", f"scale-{name}"], env=env, cwd=REPO,
        stdout=worker_log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{args.port}"
    headers = {"X-TraceX-Actor": "scale-benchmark"}
    try:
        for _ in range(60):
            try:
                _get(base, "/v1/healthz", headers)
                break
            except OSError:
                time.sleep(0.5)
        request = urllib.request.Request(
            base + "/v1/cases", data=json.dumps({"name": f"scale {name}", "synthetic": True}).encode(),
            headers={**headers, "Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request) as response:
            case_id = json.loads(response.read())["case_id"]
        started = time.time()
        upload = subprocess.run(
            ["curl", "-sS", "-X", "POST", f"{base}/v1/cases/{case_id}/imports", "-H", "X-TraceX-Actor: scale-benchmark",
             "-H", f"Idempotency-Key: scale-{name}", "-F", f"file=@{args.source}"],
            capture_output=True, text=True, check=True,
        )
        job_id = json.loads(upload.stdout)["job_id"]
        upload_seconds = time.time() - started
        stages: dict[str, float] = {}
        peak = 0
        job: dict = {}
        while True:
            peak = max(peak, _rss_mb(worker.pid))
            job = _get(base, f"/v1/jobs/{job_id}", headers)
            stages.setdefault(job["stage"], round(time.time() - started, 1))
            if job["state"] in {"completed", "failed"}:
                break
            if worker.poll() is not None:
                job["worker_exit_code"] = worker.returncode
                break
            time.sleep(1)
        total = round(time.time() - started, 1)
        result = {
            "source": str(args.source), "source_bytes": args.source.stat().st_size,
            "state": job.get("state"), "error": job.get("error_detail"), "worker_exit_code": job.get("worker_exit_code"),
            "rows_seen": job.get("rows_seen"), "rows_accepted": job.get("rows_accepted"),
            "rows_quarantined": job.get("rows_quarantined"), "upload_seconds": round(upload_seconds, 1),
            "total_seconds": total, "stage_started_at_seconds": stages, "worker_peak_rss_mb": peak,
            "machine": _get(base, "/v1/healthz", headers).get("resources"),
        }
        if job.get("state") == "completed":
            result["findings"] = _get(base, f"/v1/cases/{case_id}/findings/summary", headers)["total"]
            entities = _get(base, f"/v1/cases/{case_id}/entities?limit=1", headers)
            result["analytics"] = {key: entities["summary"].get(key) for key in (
                "entity_count", "clustered_addresses", "wallet_count", "embedded_wallets", "relay_concentrations",
                "execution_mode", "timings_s")}
        print(json.dumps(result, indent=2))
        (work / "result.json").write_text(json.dumps(result, indent=2))
    finally:
        for process in (worker, api):
            process.send_signal(signal.SIGTERM)
        for process in (worker, api):
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
        worker_log.close()
        if not args.keep:
            shutil.rmtree(work / "evidence", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
