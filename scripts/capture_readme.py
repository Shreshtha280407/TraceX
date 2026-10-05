"""Record the real UI on a disposable local stack; never use an owner database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/readme")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tracex-readme-", dir="/tmp"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "TRACEX_DATABASE_URL": f"sqlite:///{work / 'control.sqlite'}",
        "TRACEX_EVIDENCE_ROOT": str(work / "evidence"),
        "TRACEX_GEOIP_DIR": str(work / "geoip"),
        "TRACEX_WEB_DIR": str(ROOT / "frontend/dist"),
        "TRACEX_ENV": "development",
        "TRACEX_SECRET_KEY": secrets.token_hex(32),
        "TRACEX_ALLOW_DEV_ACTOR_HEADER": "0",
        "TRACEX_CANDIDATE_DIRECTORY": "",
        "TRACEX_CANDIDATE_MANIFEST_SHA256": "",
        "TRACEX_ML_FINDINGS": "1",
        "TRACEX_ML_THREADS": "1",
        "TRACEX_WORKERS": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    if not args.skip_build:
        subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend", env={**env, "VITE_API_BASE_URL": ""}, check=True)
    subprocess.run([sys.executable, "-m", "scripts.bounded_study_fixture", "--output", str(work / "fixture"),
                    "--rows", "1000", "--seed", "tracex-readme-cinematic-20261005"], cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "-c", "from app.db import init_database; init_database()"], cwd=ROOT, env=env, check=True)
    logs = []
    processes = []
    try:
        for name, command in [
            ("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]),
            ("worker", [sys.executable, "-m", "workers.runner"]),
        ]:
            log = (work / f"{name}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                with urlopen(f"{base}/v1/readyz", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, ValueError):
                time.sleep(0.5)
        else:
            raise RuntimeError(f"Capture stack not ready; see {work}")
        print(f"Disposable capture stack ready: {base}; logs: {work}", flush=True)
        source = work / "fixture/ingestion_rows.ndjson"
        subprocess.run(["node", "e2e/readme-recording.mjs"], cwd=ROOT / "frontend", env={
            **env, "TRACEX_RECORD_BASE": base, "TRACEX_RECORD_SOURCE": str(source),
            "TRACEX_RECORD_OUTPUT": str(output), "TRACEX_RECORD_WORK": str(work),
        }, check=True)
        report_path = output / "capture-manifest.json"
        report = json.loads(report_path.read_text())
        for clip in report["clips"]:
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(output / clip["file"])], text=True))
            clip["duration_seconds"] = float(probe["format"]["duration"])
        report.update({"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                       "source_rows": sum(1 for _ in source.open()),
                       "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                       "database": "disposable SQLite; not PostgreSQL or scale acceptance",
                       "geoip": "No cache installed in this recording; missing enrichment remains visible",
                       "temporary_workspace": str(work), "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        for clip in ("intake", "graph", "review"):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(output / f"{clip}.webm"),
                "-vf", "fps=7,scale=880:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=80[p];[s1][p]paletteuse=dither=none:diff_mode=rectangle",
                "-loop", "0", str(output / f"{clip}.gif")], check=True)
        for name in ("intake.webm", "graph.webm", "review.webm", "intake.gif", "graph.gif", "review.gif",
                     "landing.png", "dashboard.png", "graph.png", "evidence.png", "groups.png", "network.png", "export.png"):
            file = output / name
            report.setdefault("assets", {})[name] = {"bytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print("Real UI recordings and GIF previews complete.", flush=True)
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
