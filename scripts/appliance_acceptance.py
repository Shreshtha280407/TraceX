"""Authenticated streamed upload through an existing copied offline appliance.

Run as `uv run python -m scripts.appliance_acceptance --help`. Never starts
host-code workers, resets databases, emits passwords/tokens or deletes cases.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import inspect
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from scripts.scale_benchmark import REPO, _environment, acceptance_errors, observed_max
from scripts.scratch_usage_probe import scratch_usage


def request(base, endpoint, *, token=None, data=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + "/v1" + endpoint, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict):
        raise TypeError(f"invalid response object: {endpoint}")
    return result


def upload(base, case_id, source, token, *, timeout):
    """Bounded multipart streaming; authorization never appears in subprocess argv."""
    parsed = urllib.parse.urlsplit(base)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=timeout)
    boundary = "TraceXReview" + uuid.uuid4().hex
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
              'filename="ingestion_rows.ndjson"\r\nContent-Type: application/x-ndjson\r\n\r\n').encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    try:
        connection.putrequest("POST", f"/v1/cases/{case_id}/imports")
        connection.putheader("Authorization", "Bearer " + token)
        connection.putheader("Idempotency-Key", "offline-" + uuid.uuid4().hex)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(prefix) + source.stat().st_size + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        with source.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        body = response.read()
        if response.status != 202:
            raise RuntimeError(f"upload HTTP {response.status}: {body[:300]!r}")
        result = json.loads(body)
        if not isinstance(result, dict) or not result.get("job_id"):
            raise ValueError("invalid upload response")
        return result
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--compose-override", type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--context", default="default")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-counts", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--min-transactions", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--expected-release", default="anomaly-stack-v2")
    parser.add_argument("--scoring-mode", choices=["auto_eligible", "unsupervised", "synthetic_demo", "validated_candidate"], default="unsupervised")
    parser.add_argument("--candidate-domain")
    parser.add_argument("--user", help="Existing case owner; password requested securely, never logged")
    args = parser.parse_args(argv)
    if not args.name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in args.name):
        parser.error("unsafe run name")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    work = REPO / "var/appliance-runs" / args.name
    work.mkdir(parents=True, exist_ok=False)
    report = {"status": "failed", "scope": "copied-image API and worker, PostgreSQL, bearer authentication",
              "base": args.base, "acceptance_errors": [], "run_id": args.name,
              "target_seconds": args.max_seconds, "diagnostic_timeout_seconds": args.timeout,
              "minimum_canonical_transactions": args.min_transactions}
    docker = ["docker", "--context", args.context]
    compose = [*docker, "compose", "-p", args.project, "-f", str(args.compose.resolve())]
    if args.compose_override:
        compose.extend(["-f", str(args.compose_override.resolve())])
    stop, thread, identities, samples = threading.Event(), None, {}, {}
    job = {}
    overall = time.monotonic()
    try:
        report["host_harness_environment"] = _environment(args.source.resolve(strict=True), work)
        for service in ("api", "worker", "postgres"):
            identity = subprocess.check_output([*compose, "ps", "-q", service], text=True).strip()
            inspected = json.loads(subprocess.check_output([*docker, "inspect", identity]))[0]
            identities[service] = identity
            samples[service] = {"rss_peak_bytes": None, "tree_rss_peak_bytes": None, "container_usage_peak_bytes": None}
            report.setdefault("containers", {})[service] = {"image_id": inspected["Image"],
                "memory_limit": inspected["HostConfig"]["Memory"], "nano_cpus": inspected["HostConfig"]["NanoCpus"]}
        code = ('import json,sys,os,hashlib,importlib.metadata as m; from pathlib import Path; '
                'from app.config import settings; from app.ml.findings import release_identity,release_manifest_sha256; '
                'print(json.dumps({"python":sys.version,"effective_ml_enabled":settings.ml_findings_enabled,'
                '"release_identity":release_identity(),"release_manifest_sha256":release_manifest_sha256(),'
                '"configured_candidate_manifest_sha256":settings.candidate_manifest_sha256,'
                '"calibration_sha256":{str(p):hashlib.sha256(p.read_bytes()).hexdigest() '
                'for p in sorted(Path("app/engine/calibration").glob("*.json"))},"versions":{n:m.version(n) '
                'for n in ("duckdb","pyarrow","numpy","scikit-learn","scipy","SQLAlchemy","psycopg")},'
                '"runtime_source_sha256":{str(p):hashlib.sha256(p.read_bytes()).hexdigest() '
                'for root in (Path("app"),Path("workers")) for p in sorted(root.rglob("*.py"))},'
                '"configuration":{n:os.getenv(n) for n in ("TRACEX_WORKERS","TRACEX_MEMORY_BUDGET_MB",'
                '"TRACEX_EXECUTION_MODE","TRACEX_ML_FINDINGS","TRACEX_LEASE_SECONDS","TRACEX_MAX_UPLOAD_BYTES")}}))')
        report["container_runtime"] = json.loads(subprocess.check_output(
            [*docker, "exec", identities["worker"], "python", "-c", code], text=True))
        report["postgres_version"] = subprocess.check_output(
            [*compose, "exec", "-T", "postgres", "psql", "-U", "tracex", "-d", "tracex", "-Atc", "SELECT version()"], text=True).strip()
        if args.user:
            import getpass
            auth = request(args.base, "/auth/login", data={"display_name": args.user, "password": getpass.getpass("TraceX owner password: ")})
        else:
            import secrets
            auth = request(args.base, "/auth/signup", data={"display_name": "offline-review-" + uuid.uuid4().hex,
                                                         "password": secrets.token_urlsafe(32)})
        token = auth["token"]
        case = request(args.base, "/cases", token=token, data={"name": "scale offline " + args.name, "synthetic": True,
            "scoring_mode": args.scoring_mode, "candidate_domain": args.candidate_domain})
        case_id = case["case_id"]
        report["case_id"] = case_id
        scratch_code = "import json,os,shutil; from pathlib import Path;\n" + inspect.getsource(scratch_usage)
        scratch_code += '\nprint(json.dumps(scratch_usage(Path("/data/evidence"))))'

        def sample():
            try:
                with (work / "resources.ndjson").open("x") as handle:
                    while not stop.is_set():
                        row = {"elapsed_seconds": time.monotonic() - overall,
                               "host_filesystem_free_bytes": shutil.disk_usage(REPO).free}
                        for service in ("api", "worker"):
                            values = samples[service]
                            metric = json.loads(subprocess.check_output([*docker, "exec", identities[service],
                                "python", "-m", "scripts.runtime_probe", "--metrics-only"], text=True, timeout=10))
                            values["rss_peak_bytes"] = observed_max(values["rss_peak_bytes"], metric["pid1_rss_bytes"])
                            values["tree_rss_peak_bytes"] = observed_max(values["tree_rss_peak_bytes"], metric["pid1_tree_rss_bytes"])
                            current = metric["cgroup"].get("memory.current")
                            current = int(current) if current and current.isdigit() else None
                            values["container_usage_peak_bytes"] = observed_max(values["container_usage_peak_bytes"], current)
                            row[service] = metric
                        row["postgres"] = {"rss_bytes": None, "reason": "no Python/psutil in PostgreSQL image; cgroup lifetime peak retained"}
                        # Five-second metadata-only scratch/spill observations;
                        # no raw evidence reads or network services are added.
                        if int(row["elapsed_seconds"]) % 5 == 0:
                            row["scratch"] = json.loads(subprocess.check_output(
                                [*docker, "exec", identities["worker"], "python", "-c", scratch_code],
                                text=True, timeout=20))
                        handle.write(json.dumps(row) + "\n")
                        handle.flush()
                        stop.wait(1)
            except Exception as error:  # noqa: BLE001 - sampling failure is an acceptance error
                report["sampling_error"] = str(error)

        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        started = time.monotonic()
        deadline = started + args.timeout
        imported = upload(args.base, case_id, args.source, token, timeout=min(args.timeout, 600))
        job_id = imported["job_id"]
        report.update(job_id=job_id, upload_seconds=time.monotonic() - started)
        with (work / "progress.ndjson").open("x") as progress:
            prior = None
            while time.monotonic() < deadline:
                job = request(args.base, f"/jobs/{job_id}", token=token, timeout=min(30, max(.01, deadline - time.monotonic())))
                if not {"state", "stage", "rows_seen"}.issubset(job):
                    raise ValueError("job response missing required fields")
                state = (job["state"], job["stage"], job["rows_seen"])
                if state != prior:
                    progress.write(json.dumps({"elapsed_seconds": time.monotonic() - started, "job": job}) + "\n")
                    progress.flush()
                    print(json.dumps({"state": job["state"], "stage": job["stage"], "rows_seen": job["rows_seen"]}), flush=True)
                    prior = state
                if job["state"] in {"completed", "failed"}:
                    break
                if report.get("first_provisional_activity_seconds") is None and job.get("rows_accepted", 0):
                    activity = request(args.base, f"/cases/{case_id}/activity?job_id={job_id}&limit=1", token=token,
                        timeout=min(10, max(.01, deadline - time.monotonic())))
                    if activity.get("entities"):
                        report["first_provisional_activity_seconds"] = time.monotonic() - started
                time.sleep(2)
            else:
                raise TimeoutError("offline appliance acceptance timeout; live case retained")
        remaining = lambda: min(30, max(.01, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise TimeoutError("deadline before authenticated final findings retrieval")
        findings = request(args.base, f"/cases/{case_id}/findings?limit=1", token=token, timeout=remaining())
        if "findings" not in findings or "total" not in findings:
            raise ValueError("final findings retrieval returned an invalid schema")
        retrieval_started = time.monotonic()
        groups = request(args.base, f"/cases/{case_id}/investigation-queue?capacity=100&limit=1", token=token, timeout=remaining())
        if not groups.get("items") or groups.get("underlying_findings") != findings["total"]:
            raise ValueError("final group retrieval/count completeness failed")
        group_id = groups["items"][0]["group_id"]
        group_detail = request(args.base, f"/investigation-groups/{group_id}", token=token, timeout=remaining())
        group_members = request(args.base, f"/investigation-groups/{group_id}/members?limit=1", token=token, timeout=remaining())
        if not group_members.get("items") or group_detail.get("snapshot_id") != job.get("snapshot_id"):
            raise ValueError("final group evidence is absent or belongs to another snapshot")
        report["final_group_retrieval"] = {key: groups[key] for key in ("underlying_findings", "investigation_groups", "unresolved_groups", "queued_groups", "backlog_groups", "policy")}
        report["final_group_retrieval"].update(authenticated=True, returned_member_evidence=True,
            duration_seconds=time.monotonic() - retrieval_started, grouping_version=group_detail["grouping_version"],
            grouping_sha256=group_detail["procedure_sha256"])
        seconds = time.monotonic() - started
        if seconds >= args.timeout:
            raise TimeoutError("deadline exceeded during final findings retrieval")
        report["final_findings_retrieval"] = {"total": findings["total"], "returned": len(findings["findings"]),
            "snapshot_ids": sorted({f["snapshot_id"] for f in findings["findings"]}), "authenticated": True}
        report["first_useful_output_seconds"] = report.get("first_provisional_activity_seconds")
        report["first_useful_output_scope"] = "receipt-approved provisional address participation, not final graph conclusions; null if not observed by polling"
        report["clock_contract"] = "fresh upload initiation through terminal snapshot and authenticated final investigation group/member evidence retrieval; no resume"
        inspection = request(args.base, f"/cases/{case_id}/analysis?job_id={job_id}", token=token)
        inspection["geoip"] = request(args.base, "/geoip/status", token=token)
        sources = request(args.base, f"/cases/{case_id}/sources", token=token)
        with args.source.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        report.update(job=job, inspection=inspection, total_seconds=seconds, source_sha256=checksum)
        report["expected_counts"] = {"values": json.loads(args.expected_counts.read_text()),
            "sha256": hashlib.sha256(args.expected_counts.read_bytes()).hexdigest()}
        provenance = args.expected_counts.with_suffix(".provenance.json")
        if provenance.exists():
            report["count_provenance"] = json.loads(provenance.read_text())
        errors = acceptance_errors(job, inspection,
            required_stages=["source_verification", "ingesting", "graph_building", "findings", "ml_scoring", "analytics", "investigation_grouping"],
            expected_counts=json.loads(args.expected_counts.read_text()), min_transactions=args.min_transactions,
            max_seconds=args.max_seconds, seconds=seconds, expected_release=args.expected_release)
        if (job.get("analysis") or {}).get("state") != "complete":
            errors.append("appliance analysis is not complete; incomplete coverage is not an acceptance pass")
        if findings["total"] <= 0:
            errors.append("no final findings retrievable")
        if any(row["snapshot_id"] != job.get("snapshot_id") for row in findings["findings"]):
            errors.append("retrieved findings do not belong to the final job snapshot")
        if not any(source["sha256"] == checksum for source in sources["sources"]):
            errors.append("immutable uploaded source hash mismatch")
        if report.get("count_provenance", {}).get("source_sha256") != checksum:
            errors.append("independent expected-count provenance missing or source hash mismatch")
        if report["container_runtime"]["runtime_source_sha256"] != report["host_harness_environment"]["runtime_source_sha256"]:
            errors.append("runtime worker code identity differs from checkout")
        if report.get("sampling_error"):
            errors.append("resource sampler failed")
        report.update(acceptance_errors=errors, status="failed" if errors else "pass")
        time_missed = args.max_seconds is not None and seconds >= args.max_seconds
        report["time_target_status"] = "TIME TARGET MISSED" if time_missed else "MET" if args.max_seconds is not None else "NOT SPECIFIED"
        only_time = errors and all("must be less than target" in error for error in errors)
        report["acceptance_outcome"] = "TIME TARGET MISSED" if only_time else "FAILED" if errors else "PASS"
    except Exception as error:  # noqa: BLE001 - every failure retained, never count as success
        report["error"] = f"{type(error).__name__}: {error}"
        report["acceptance_errors"].append(report["error"])
    finally:
        stop.set()
        if thread:
            thread.join(timeout=5)
        report.update(job=job, resource_peaks=samples, elapsed_seconds=time.monotonic() - overall,
                      memory_measurement="sampled INSIDE actual Linux API/worker; child RSS sums may double-count; unsupported metrics are null")
        for service, identity in identities.items():
            try:
                log = subprocess.run([*docker, "logs", "--tail", "500", identity], capture_output=True, text=True,
                                     check=False, timeout=30)
                (work / f"{service}.log").write_text(log.stdout + log.stderr)
                peak = subprocess.run([*docker, "exec", identity, "cat", "/sys/fs/cgroup/memory.peak"],
                                      capture_output=True, text=True, check=False, timeout=10)
                report.setdefault("container_cgroup_peak_bytes", {})[service] = peak.stdout.strip() if peak.returncode == 0 else None
            except Exception as error:  # noqa: BLE001 - diagnostics must not prevent the final report
                report.setdefault("diagnostic_errors", {})[service] = str(error)
        report["cgroup_peak_scope"] = "container lifetime; may include preceding cases, not reset per benchmark"
        report["scratch_measurement"] = "~5s sampled logical .work/.staging sizes across review cases; sampling overhead included"
        report["exit_code"] = int(report["status"] != "pass")
        stages = (job.get("analysis") or {}).get("stages", [])
        timed = [stage for stage in stages if isinstance(stage.get("duration_seconds"), (float, int))]
        report["bottleneck"] = max(timed, key=lambda stage: stage["duration_seconds"]) if timed else {"status": "not available; inspect progress/stage details and logs"}
        with (work / "result.json").open("x") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(json.dumps({"status": report["status"], "result": str(work / "result.json"),
                          "seconds": report.get("total_seconds"), "errors": report["acceptance_errors"]}), flush=True)
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
