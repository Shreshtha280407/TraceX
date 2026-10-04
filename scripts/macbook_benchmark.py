"""Ordered native Docker/PostgreSQL benchmark orchestration. Large commands run ONLY when explicitly invoked.

Use --help and subcommand --help. Analysis has no cloud runtime dependency.
Preparation may download pinned dependencies, images and licensed Geo-IP files.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.telemetry import host_metrics

ROOT = Path(__file__).resolve().parents[1]


def architecture(machine):
    aliases = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    if machine.lower() not in aliases:
        raise ValueError(f"unsupported CPU architecture: {machine}")
    return aliases[machine.lower()]


def execute(command, **kwargs):
    return subprocess.run(command, check=True, cwd=ROOT, **kwargs)


def docker(args, *command):
    return ["docker", "--context", args.context, *command]


def compose(args, *command):
    files = ["-f", str(args.install.resolve() / "docker-compose.yml")]
    candidate = args.install.resolve() / "docker-compose.candidate.yml"
    if candidate.is_file():
        files.extend(["-f", str(candidate)])
    return docker(args, "compose", "-p", args.project, *files, *command)


def output(command):
    return subprocess.check_output(command, text=True, timeout=30, cwd=ROOT)


def preflight(args):
    host = host_metrics()
    errors, warnings = [], []
    vm = json.loads(output(docker(args, "info", "--format", "{{json .}}")))
    worker = output(compose(args, "ps", "-q", "worker")).strip()
    if not worker:
        raise ValueError("worker is not running; run start first")
    runtime = json.loads(output(docker(args, "exec", worker, "python", "-m", "scripts.runtime_probe",
        "--counts-json", json.dumps(json.loads(args.counts.read_text()) if args.counts else {
            "transactions": 3_050_000, "outputs": 8_050_000, "inputs": 4_550_000}))))
    inspected = json.loads(output(docker(args, "inspect", worker)))[0]
    image = json.loads(output(docker(args, "image", "inspect", inspected["Image"])))[0]
    native = architecture(platform.machine())
    if image["Architecture"] != native or architecture(vm["Architecture"]) != native:
        errors.append("native image/VM architecture mismatch: silent amd64 emulation is not accepted")
    available = runtime["effective_available_bytes"]
    if available is None:
        errors.append("actual worker available memory is unsupported; strict large-run admission cannot be verified")
    budget = runtime["resource_plan"]["memory_budget_bytes"]
    ceiling = min(budget, int(available * .8)) if available is not None else budget
    estimates = runtime["estimates"]
    if max(estimates[key] for key in ("ml_joint_bytes", "analytics_joint_bytes", "risk_joint_bytes")) > ceiling:
        errors.append("global/native/scratch memory admission rejected; choose a safe budget/VM allocation, do not skip a stage")
    if not runtime["ml_enabled"]:
        errors.append("ML stage disabled")
    if runtime.get("candidate_configured") and runtime.get("candidate_admission", {}).get("status") != "verified_metadata":
        errors.append("configured candidate integrity/working-set admission is not verified; restore the pinned compatible artifact")
    if runtime["requested_workers"] and int(runtime["requested_workers"]) != runtime["effective_workers"]:
        errors.append("requested workers exceed joint CPU/native-memory admission; choose a supported configuration")
    if not runtime["geoip_files"] or not any(name.endswith("manifest.json") for name in runtime["geoip_files"]):
        errors.append("required offline Geo-IP/ASN files missing")
    if runtime.get("geoip_integrity", {}).get("status") != "verified":
        errors.append("offline Geo-IP/ASN checksum inventory is not verified")
    services = {"worker": {"image_architecture": image["Architecture"], "memory_limit": inspected["HostConfig"]["Memory"]}}
    for service in ("api", "postgres"):
        identity = output(compose(args, "ps", "-q", service)).strip()
        container = json.loads(output(docker(args, "inspect", identity)))[0]
        service_image = json.loads(output(docker(args, "image", "inspect", container["Image"])))[0]
        services[service] = {"image_architecture": service_image["Architecture"], "memory_limit": container["HostConfig"]["Memory"], "image_id": container["Image"]}
        if service_image["Architecture"] != native:
            errors.append(f"{service} is emulated rather than native")
        if service == "api":
            api_runtime = json.loads(output(docker(args, "exec", identity, "python", "-m", "scripts.runtime_probe", "--counts-json", "{}")))
            if api_runtime["code_sha256"] != runtime["code_sha256"]:
                errors.append("API and worker code identities differ")
    postgres_version = output(compose(args, "exec", "-T", "postgres", "psql", "-U", "tracex", "-d", "tracex", "-Atc", "SELECT version()" )).strip()
    # df is executed in the actual PostgreSQL Linux container, including Docker Desktop.
    pg_df = output(compose(args, "exec", "-T", "postgres", "df", "-Pk", "/var/lib/postgresql/data"))
    pg_free = int(pg_df.strip().splitlines()[-1].split()[3]) * 1024
    expected = {str(path.relative_to(ROOT)): __import__("hashlib").sha256(path.read_bytes()).hexdigest()
                for root in (ROOT / "app", ROOT / "workers") for path in sorted(root.rglob("*.py"))}
    if runtime["code_sha256"] != expected:
        errors.append("worker image code does not match the current checkout")
    source_bytes = args.source.stat().st_size if args.source else estimates["scratch_bytes"] // 4
    # Generator canonical/raw/truth files, retained vault + SQL scratch + PG WAL.
    disk_required = estimates["scratch_bytes"] * 2 + source_bytes * 4 + (8 << 30)
    host_disk = shutil.disk_usage(args.install)
    if runtime["evidence_filesystem"]["free"] < disk_required:
        errors.append("Docker evidence filesystem lacks conservative source/scratch/WAL/reserve space")
    if host_disk.free < disk_required:
        errors.append("host filesystem lacks conservative preparation/run space")
    if pg_free < disk_required:
        errors.append("actual PostgreSQL/Docker filesystem lacks conservative run space")
    physical = host["memory"]["physical_bytes"]
    if physical and vm["MemTotal"] > physical * .80:
        warnings.append("Docker VM allocation exceeds 80% of physical RAM; macOS pressure may be severe")
    recommendations = {"worker_budget_ceiling_mb": ceiling >> 20,
        "docker_vm_upper_recommendation_mb": int(min(physical * .72, (host["memory"]["available_bytes"] or physical) * .85)) >> 20 if physical else None,
        "basis": "time-varying availability; retain macOS and PostgreSQL/API headroom; do not force a fixed Docker allocation"}
    from scripts.appliance_acceptance import request
    readiness = request(args.base, "/readyz", timeout=10)
    return {"status": "BLOCKED" if errors else "ADMITTED_NOT_RUN", "large_run": "NOT RUN", "errors": errors,
        "warnings": warnings, "host": host, "docker_vm": {key: vm.get(key) for key in ("Architecture", "MemTotal", "NCPU", "ServerVersion")},
        "worker": runtime, "image": {key: image.get(key) for key in ("Id", "Architecture", "Os", "RepoTags")},
        "container_memory_limit": inspected["HostConfig"]["Memory"], "disk_required_bytes": disk_required,
        "containers": services, "postgres_version": postgres_version, "postgres_filesystem_free_bytes": pg_free,
        "recommendations": recommendations, "api_readiness": readiness,
        "network_scope": "Mac loopback UI routing is NOT native-Linux strict internal-network isolation evidence"}


def run_acceptance(args, name):
    from scripts.appliance_acceptance import main
    return main(["--base", args.base, "--compose", str(args.install / "docker-compose.yml"),
        "--project", args.project, "--context", args.context, "--source", str(args.source),
        "--expected-counts", str(args.counts), "--name", name, "--timeout", str(args.timeout),
        "--min-transactions", str(args.minimum), "--max-seconds", str(args.target),
        "--expected-release", args.expected_release, "--scoring-mode", args.scoring_mode,
        *(["--user", args.user] if args.user else []),
        *(["--compose-override", str(args.install / "docker-compose.candidate.yml")] if (args.install / "docker-compose.candidate.yml").is_file() else []),
        *(["--candidate-domain", args.candidate_domain] if args.candidate_domain else [])])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="default")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="exclusive isolated compose/env; does NOT build or start")
    init.add_argument("--install", type=Path, required=True)
    init.add_argument("--image", required=True)
    init.add_argument("--memory-mb", type=int, required=True)
    init.add_argument("--workers", type=int, default=2)
    init.add_argument("--port", type=int, default=8000)
    build = sub.add_parser("build", help="ONLINE native architecture image build")
    build.add_argument("--image", required=True)
    build.add_argument("--candidate-backends", action="store_true", help="optional XGBoost/LightGBM runtime; online image preparation only")
    for command in ("start", "configure", "candidate", "preflight", "screen", "accept", "profile"):
        p = sub.add_parser(command)
        p.add_argument("--install", type=Path, required=True)
        p.add_argument("--project", required=True)
        p.add_argument("--base", default="http://127.0.0.1:8000")
        if command not in {"start", "configure", "candidate"}:
            p.add_argument("--source", type=Path, required=command in {"accept", "screen", "profile"})
            p.add_argument("--counts", type=Path, required=command in {"accept", "screen", "profile"})
            p.add_argument("--output", type=Path, required=True)
        if command in {"accept", "screen", "profile"}:
            p.add_argument("--minimum", type=int, default=3_000_000)
            p.add_argument("--target", type=float, default=1800)
            p.add_argument("--timeout", type=float, default=7200)
            p.add_argument("--expected-release", default="anomaly-stack-v2")
            p.add_argument("--scoring-mode", choices=["auto_eligible", "unsupervised", "synthetic_demo", "validated_candidate"], default="unsupervised")
            p.add_argument("--candidate-domain")
            p.add_argument("--user", help="Existing UI owner; password requested securely for each fresh run")
        if command == "screen":
            p.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
        if command == "configure":
            p.add_argument("--workers", type=int, required=True)
        if command == "candidate":
            p.add_argument("--artifact", type=Path, required=True)
            p.add_argument("--manifest-sha256", required=True)
    generation = sub.add_parser("generate", help="LARGE preparation; NEVER invoked by preflight")
    generation.add_argument("--rows", type=int, default=3_000_000)
    generation.add_argument("--seed", required=True)
    generation.add_argument("--output", type=Path, required=True)
    generation.add_argument("--workers", type=int, default=2)
    count = sub.add_parser("count")
    count.add_argument("--source", type=Path, required=True)
    count.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if hasattr(args, "output") and args.output.exists():
        parser.error("output must be new; no previous benchmark is overwritten")
    if args.command == "init":
        if args.memory_mb < 512 or args.workers < 1:
            parser.error("positive workers and at least 512 MB required; full 3M admission is checked separately")
        args.install.mkdir(parents=True, exist_ok=False)
        shutil.copy2(ROOT / "deploy/appliance/docker-compose.yml", args.install / "docker-compose.yml")
        values = {"TRACEX_SECRET_KEY": secrets.token_hex(32), "TRACEX_DB_PASSWORD": secrets.token_hex(24),
            "TRACEX_IMAGE": args.image, "TRACEX_MEMORY_BUDGET_MB": str(args.memory_mb), "TRACEX_WORKERS": str(args.workers),
            "TRACEX_PORT": str(args.port), "TRACEX_UI_INTERNAL": "false", "TRACEX_ML_FINDINGS": "1",
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
        from scripts.prepare_review_install import review_environment
        environment = review_environment((ROOT / "deploy/appliance/.env.example").read_text(), values)
        with (args.install / ".env").open("x") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(environment)
        (args.install / ".tracex-benchmark.json").write_text(json.dumps({"schema": 1, "purpose": "isolated owner-invoked benchmark", "image": args.image}))
        print(json.dumps({"install": str(args.install), "status": "PREPARED_NOT_RUN", "architecture": architecture(platform.machine())}))
        return 0
    if args.command == "build":
        execute(docker(args, "build", "--platform", "linux/" + architecture(platform.machine()),
            "--build-arg", "TRACEX_CANDIDATE_BACKENDS=" + str(int(args.candidate_backends)), "-t", args.image, "."))
        return 0
    if hasattr(args, "project") and (not args.project.startswith("tracex-benchmark-") or not (args.install / ".tracex-benchmark.json").is_file()):
        parser.error("use an init-created installation and a unique tracex-benchmark-* project; never reuse the owner's case stack")
    if args.command == "candidate":
        from app.ml.candidate import load
        load(args.artifact, args.manifest_sha256)
        override = args.install / "docker-compose.candidate.yml"
        if override.exists():
            raise FileExistsError("candidate override already exists; use a fresh installation to preserve its release identity")
        volumes = {"volumes": [{"type": "bind", "source": str(args.artifact.resolve()),
            "target": "/opt/tracex/trusted-candidate", "read_only": True}], "environment": {
            "TRACEX_CANDIDATE_DIRECTORY": "/opt/tracex/trusted-candidate",
            "TRACEX_CANDIDATE_MANIFEST_SHA256": args.manifest_sha256}}
        # Compose accepts JSON; paths cannot inject YAML or shell commands.
        override.write_text(json.dumps({"services": {"api": volumes, "worker": volumes}}, indent=2))
        execute(compose(args, "up", "-d", "--force-recreate", "api", "worker"))
        return 0
    if args.command == "configure":
        if args.workers < 1:
            parser.error("workers must be positive")
        from scripts.prepare_review_install import review_environment
        environment = args.install / ".env"
        environment.write_text(review_environment(environment.read_text(), {"TRACEX_WORKERS": str(args.workers)}))
        execute(compose(args, "up", "-d", "--force-recreate", "worker"))
        return 0
    if args.command == "start":
        execute(compose(args, "up", "-d", "--wait"))
        return 0
    if args.command == "generate":
        if args.output.exists():
            parser.error("generation must use a fresh directory; never overwrite a reserved/evaluated dataset")
        execute([sys.executable, "generator.py", "--rows", str(args.rows), "--seed", args.seed,
                 "--output", str(args.output), "--workers", str(args.workers), "--formats", "ndjson", "--verify"])
        return 0
    if args.command == "count":
        from app.ml.candidate import sha
        from scripts.dataset_acceptance_counts import counts
        values, rows = counts(args.source)
        manifest = json.loads((args.source.parent / "dataset_manifest.json").read_text())
        if manifest["files"][args.source.name]["sha256"] != sha(args.source):
            raise ValueError("generator manifest source checksum mismatch")
        declared = manifest["counts"].get("canonical_transaction_count", manifest["counts"].get("transactions"))
        if declared != values["transactions"]:
            raise ValueError(f"independent canonical count {values['transactions']} != manifest {declared}")
        with args.output.open("x") as stream:
            json.dump(values, stream, indent=2)
        with args.output.with_suffix(".provenance.json").open("x") as stream:
            json.dump({"method": "independent SQLite disk-indexed raw row counting; truth not read", "counts": values,
                "source_rows": rows, "source_sha256": sha(args.source), "manifest_sha256": sha(args.source.parent / "dataset_manifest.json")}, stream, indent=2)
        return 0
    try:
        report = preflight(args)
    except Exception as error:  # noqa: BLE001 - failed admission diagnostics must be retained
        report = {"status": "BLOCKED", "large_run": "NOT RUN", "errors": [f"{type(error).__name__}: {error}"],
                  "scope": "preflight failed before workload execution"}
    if args.command == "preflight":
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2)
        print(json.dumps({"status": report["status"], "errors": report["errors"], "output": str(args.output)}))
        return int(bool(report["errors"]))
    if report["errors"]:
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2)
        return 1
    run = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    if args.command == "accept":
        code = run_acceptance(args, run)
        with args.output.open("x") as stream:
            json.dump({"run_id": run, "exit_code": code, "result": str(ROOT / "var/appliance-runs" / run / "result.json"), "preflight": report}, stream, indent=2)
        return code
    if args.command == "profile":
        # Profiling is a separate fresh run, never substitute for final acceptance.
        override = args.install / ("profile-" + run + ".yml")
        override.write_text('services:\n  worker:\n    command: ["tracex-worker", "--worker-id", "profile-worker", "--profile-output", "/data/evidence/profile-' + run + '.pstats"]\n')
        execute(compose(args, "-f", str(override), "up", "-d", "worker"))
        diagnostics = []
        code = 1
        try:
            code = run_acceptance(args, "profile-" + run)
        except Exception as error:  # noqa: BLE001
            diagnostics.append(str(error))
        finally:
            # Diagnostics must survive missing profiles and worker restoration errors.
            try:
                identity = output(compose(args, "ps", "-aq", "worker")).strip()
                execute(docker(args, "cp", identity + ":/data/evidence/profile-" + run + ".pstats", str(args.output.with_suffix(".pstats"))))
            except Exception as error:  # noqa: BLE001
                diagnostics.append(str(error))
            try:
                execute(compose(args, "up", "-d", "worker"))
            except Exception as error:  # noqa: BLE001
                diagnostics.append(str(error))
            with args.output.open("x") as stream:
                json.dump({"run_id": "profile-" + run, "exit_code": code, "errors": diagnostics, "preflight": report,
                    "result": str(ROOT / "var/appliance-runs" / ("profile-" + run) / "result.json"), "scope": "separate fresh profiled run; parent profile only, not child/native CPU"}, stream, indent=2)
        return code
    from scripts.prepare_review_install import review_environment
    environment = args.install / ".env"
    original = environment.read_text()
    results = []
    try:
        for workers in args.workers:
            environment.write_text(review_environment(original, {"TRACEX_WORKERS": str(workers)}))
            execute(compose(args, "up", "-d", "--force-recreate", "worker"))
            check = preflight(args)
            name = "screen-" + run + "-w" + str(workers)
            if check["errors"]:
                results.append({"workers": workers, "status": "BLOCKED", "errors": check["errors"]})
                continue
            code = run_acceptance(args, name)
            result = json.loads((ROOT / "var/appliance-runs" / name / "result.json").read_text())
            results.append({"workers": workers, "exit_code": code, "seconds": result.get("total_seconds"), "result": name})
    finally:
        environment.write_text(original)
        execute(compose(args, "up", "-d", "--force-recreate", "worker"))
    passed = [value for value in results if value.get("exit_code") == 0]
    fastest = min((value["seconds"] for value in passed), default=None)
    selected = min((value["workers"] for value in passed if value["seconds"] <= fastest * 1.05), default=None) if fastest else None
    with args.output.open("x") as stream:
        json.dump({"results": results, "selected_safe_workers": selected, "policy": "fewest workers within 5% of fastest passing full-stage fresh screening run; env restored"}, stream, indent=2)
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
