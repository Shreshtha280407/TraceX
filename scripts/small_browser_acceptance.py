"""Isolated 64-transaction SQLite/browser wiring check, NEVER large acceptance.

Creates a fresh disposable-by-design directory but does not delete it. Existing
servers, datasets, case stores and Docker are untouched. Requires built web UI,
Playwright and Chrome (online installation is separate preparation).
"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from scripts.quality_matrix import independent_fixture

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    work = args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    independent_fixture(work / "fixture", 64, 1404, "browser-independent")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TRACEX_DATABASE_URL": f"sqlite:///{work / 'control.db'}", "TRACEX_EVIDENCE_ROOT": str(work / "evidence"),
        "TRACEX_SECRET_KEY": secrets.token_hex(32), "TRACEX_ENV": "test", "TRACEX_ALLOW_DEV_ACTOR_HEADER": "0",
        "TRACEX_WEB_DIR": str(work / "web"), "TRACEX_GEOIP_DIR": str(ROOT / "var/geoip"),
        "TRACEX_EXECUTION_MODE": "bounded", "TRACEX_MEMORY_BUDGET_MB": "2048", "TRACEX_WORKERS": "1", "TRACEX_ML_FINDINGS": "1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
    # Do not accidentally activate an owner's unrelated candidate environment.
    env.pop("TRACEX_CANDIDATE_DIRECTORY", None)
    env.pop("TRACEX_CANDIDATE_MANIFEST_SHA256", None)
    processes, handles = [], []
    result = {"status": "failed", "scope": "64-row independently authored SQLite/authenticated browser check; NOT PostgreSQL/3M acceptance"}
    try:
        # Preserve the owner's frontend .env.local; compile an isolated same-origin build.
        subprocess.run(["npm", "run", "build", "--", "--outDir", str(work / "web")], cwd=ROOT / "frontend",
            env={**env, "VITE_API_BASE_URL": ""}, check=True, timeout=60)
        subprocess.run([sys.executable, "-c", "from app.db import Base,engine; import app.models; Base.metadata.create_all(engine)"], env=env, cwd=ROOT, check=True)
        for name, command in (("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]),
                              ("worker", [sys.executable, "-m", "workers.runner", "--worker-id", "small-browser"])):
            handle = (work / (name + ".log")).open("x")
            handles.append(handle)
            processes.append(subprocess.Popen(command, env=env, cwd=ROOT, stdout=handle, stderr=handle))
        deadline = time.monotonic() + min(30, args.timeout)
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(base + "/v1/healthz", timeout=1):
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise TimeoutError("small API readiness failed")
        check = subprocess.run(["node", "e2e/acceptance.mjs"], cwd=ROOT / "frontend", timeout=args.timeout,
            env={**env, "TRACEX_E2E_BASE": base, "TRACEX_E2E_SOURCE": str(work / "fixture/ingestion_rows.ndjson"), "TRACEX_E2E_OUTPUT": str(work / "browser")}, check=False)
        result["browser"] = json.loads((work / "browser/result.json").read_text())
        result["status"] = "pass" if check.returncode == 0 else "failed"
    except Exception as error:  # noqa: BLE001 - retain all small-check diagnostics
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()
        with (work / "result.json").open("x") as stream:
            json.dump(result, stream, indent=2)
    return int(result["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
