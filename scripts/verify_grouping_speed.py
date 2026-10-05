"""Fresh small CSV upload through the local API/worker; no large-data benchmark.

Creates one isolated synthetic test account/case and retains its evidence. A
unique compact report is always saved. Real completion, not a mocked delay, is
measured. No credentials or session tokens are written to the report.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8001")
    parser.add_argument("--frontend-base", default="http://localhost:5173")
    parser.add_argument("--rows", type=int, default=1000, help="3..10000 small canonical fixture transactions")
    parser.add_argument("--deadline-seconds", type=int, default=180, help="finite upload-to-completion diagnostic timeout")
    parser.add_argument("--browser", action="store_true", help="also verify real queue navigation/review in installed Chrome")
    parser.add_argument("--output", type=Path, help="new directory; existing reports are never overwritten")
    args = parser.parse_args()
    if not 3 <= args.rows <= 10000 or not 10 <= args.deadline_seconds <= 600:
        parser.error("rows must be 3..10000 and deadline 10..600 seconds")
    for base in (args.api_base, args.frontend_base):
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            parser.error("functional verification is restricted to the owner's local installation")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    out = args.output or Path("var") / ("grouping-speed-live-" + run_id)
    out.mkdir(parents=True, exist_ok=False)
    report = {"run_id": run_id, "scope": "Small synthetic CSV functional/timing check only; NOT a 100K/1M benchmark or quality evaluation",
              "requested_transactions": args.rows, "checks": []}
    token = None

    def check(name, ok):
        report["checks"].append({"name": name, "ok": bool(ok)})
        if not ok:
            raise AssertionError(name)
        print("PASS " + name, flush=True)

    def request(path, body=None, content_type="application/json", authenticated=True):
        headers = {"Content-Type": content_type}
        if token and authenticated:
            headers["Authorization"] = "Bearer " + token
        if path.endswith("/imports"):
            headers["Idempotency-Key"] = run_id
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        req = urllib.request.Request(args.api_base.rstrip("/") + "/v1" + path, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)

    try:
        auth = request("/auth/signup", {"display_name": "group-speed-" + run_id, "password": secrets.token_urlsafe(32)})
        token = auth["token"]
        case = request("/cases", {"name": "Grouping speed CSV fixture " + run_id,
                                  "synthetic": True, "scoring_mode": "unsupervised"})
        case_id = case["case_id"]
        csv_text = io.StringIO(newline="")
        writer = csv.DictWriter(csv_text, fieldnames=["txid", "network", "timestamp", "inputs", "outputs", "fee_sats"])
        writer.writeheader()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for n in range(args.rows):
            writer.writerow({"txid": f"{n + 1:064x}", "network": "bitcoin-regtest",
                "timestamp": (start + timedelta(seconds=n)).isoformat(),
                "inputs": json.dumps([{"address": "fixture-unlinked-input", "amount_sats": 100000}]),
                "outputs": json.dumps([{"address": f"fixture-recipient-{n}", "amount_sats": 99000}]), "fee_sats": 1000})
        source = csv_text.getvalue().encode()
        boundary = "group-speed-" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="group-speed.csv"\r\n'
                "Content-Type: text/csv\r\n\r\n").encode() + source + f"\r\n--{boundary}--\r\n".encode()
        clock = time.monotonic()
        uploaded = request(f"/cases/{case_id}/imports", body, "multipart/form-data; boundary=" + boundary)
        report["upload_seconds"] = time.monotonic() - clock
        job_id = uploaded["job_id"]
        deadline = clock + args.deadline_seconds
        observed_progress = []
        while time.monotonic() < deadline:
            job = request("/jobs/" + job_id)
            if job["stage"] == "investigation_grouping":
                observed_progress.append(job["progress"])
            if job["state"] in {"completed", "failed"}:
                break
            time.sleep(.2)
        else:
            raise TimeoutError("finite small CSV diagnostic timeout; the existing worker/evidence are retained")
        report.update(case_id=case_id, job_id=job_id, analysis=job["analysis"],
                      observed_group_progress=observed_progress, canonical_transactions=job["rows_accepted"],
                      source_rows=job["rows_seen"], quarantined_rows=job["rows_quarantined"],
                      source_sha256=hashlib.sha256(source).hexdigest())
        check("real CSV completed without reimport or a fabricated completion", job["state"] == "completed")
        check("exact small canonical/source counts, no quarantine", job["rows_accepted"] == args.rows
              and job["rows_seen"] == args.rows and job["rows_quarantined"] == 0)
        stages = {stage["name"]: stage for stage in job["analysis"]["stages"]}
        group_stage = stages["investigation_grouping"]
        check("real worker used the optimized frozen grouping procedure", group_stage["status"] == "complete"
              and group_stage["details"]["execution_version"] == "disk-indexed-grouping-v1")
        report["grouping_seconds"] = group_stage["duration_seconds"]
        queue = request(f"/cases/{case_id}/investigation-queue")
        group_id = queue["items"][0]["group_id"]
        detail = request("/investigation-groups/" + group_id)
        members = request("/investigation-groups/" + group_id + "/members")
        check("authenticated final queue/member retrieval and complete coverage", queue["grouping_coverage"]["state"] == "complete"
              and members["items"] and detail["procedure_sha256"] == group_stage["details"]["procedure_sha256"])
        report["upload_to_group_retrieval_seconds"] = time.monotonic() - clock
        report["counts"] = {key: queue[key] for key in ("underlying_findings", "investigation_groups", "unresolved_groups",
                                                      "queued_groups", "backlog_groups")}
        ref = members["items"][0]["finding"]["source_refs"][0]
        replay = request(f"/evidence/{ref['evidence_id']}/records?locator=" + urllib.parse.quote(ref["locator"], safe=""))
        check("immutable CSV source replay hash matches upload", replay["source_sha256"] == report["source_sha256"])
        try:
            request("/investigation-groups/" + group_id, authenticated=False)
            check("unauthenticated group access rejected", False)
        except urllib.error.HTTPError as exc:
            check("unauthenticated group access rejected", exc.code in {401, 403})
        if args.browser:
            root = Path(__file__).resolve().parents[1]
            browser_env = {**os.environ, "TRACEX_SPEED_AUTH": json.dumps(auth), "TRACEX_SPEED_CASE": case_id,
                "TRACEX_SPEED_JOB": job_id, "TRACEX_SPEED_QUEUE": str(queue["queued_groups"]),
                "TRACEX_SPEED_BROWSER_REPORT": str((out / "browser.json").resolve()),
                "TRACEX_E2E_BASE": args.frontend_base, "TRACEX_SPEED_API_BASE": args.api_base}
            try:
                browser = subprocess.run(["node", "e2e/grouping-speed.mjs"], cwd=root / "frontend", env=browser_env,
                                         capture_output=True, text=True, timeout=90, check=False)
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
                stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
                (out / "browser.log").write_text(stdout + stderr + "\nFinite browser diagnostic timeout\n")
                raise
            (out / "browser.log").write_text(browser.stdout + browser.stderr)
            check("real authenticated browser counts, evidence and group decision", browser.returncode == 0)
        report["status"] = "pass"
        return 0
    except Exception as exc:  # noqa: BLE001 - always retain diagnostic result on failure
        report.update(status="fail", error_type=type(exc).__name__, error=str(exc))
        return 1
    finally:
        (out / "result.json").write_text(json.dumps(report, indent=2))
        print("Report: " + str(out / "result.json"), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
