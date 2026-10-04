"""Portable orchestration and estimates, never calls Docker or allocates a large dataset."""
import json
from types import SimpleNamespace

import pytest

from scripts import macbook_benchmark as orchestration


def test_one_million_disk_floor_rejects_without_allocating_or_deleting(tmp_path, monkeypatch):
    from scripts import laptop_admission
    monkeypatch.setattr(laptop_admission.shutil, "disk_usage", lambda _: SimpleNamespace(free=20_000_000_000))
    before = list(tmp_path.iterdir())
    report = laptop_admission.inspect(tmp_path, 1_000_000)
    assert report["status"] == "BLOCKED" and report["large_run"] == "NOT RUN"
    assert report["disk_floor_bytes"] == 33_663_676_416
    assert report["canonical_transactions"] is None and list(tmp_path.iterdir()) == before


def test_native_arm_preflight_checks_actual_worker_budget_not_host_ram(tmp_path, monkeypatch):
    from scripts import appliance_acceptance
    monkeypatch.setattr(orchestration.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(orchestration, "host_metrics", lambda: {"memory": {"physical_bytes": 32 << 30, "available_bytes": 20 << 30}, "architecture": "arm64"})
    code = {str(path.relative_to(orchestration.ROOT)): __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            for root in (orchestration.ROOT / "app", orchestration.ROOT / "workers") for path in sorted(root.rglob("*.py"))}
    runtime = {"effective_available_bytes": 16 << 30, "resource_plan": {"memory_budget_bytes": 12 << 30},
        "estimates": {"ml_joint_bytes": 11 << 30, "analytics_joint_bytes": 5 << 30, "risk_joint_bytes": 8 << 30, "scratch_bytes": 40 << 30},
        "ml_enabled": True, "requested_workers": "2", "effective_workers": 2,
        "geoip_files": {"compiled/manifest.json": "hash"}, "geoip_integrity": {"status": "verified"}, "code_sha256": code,
        "evidence_filesystem": {"free": 500 << 30}}
    def output(command):
        if "df" in command:
            return "Filesystem 1024-blocks Used Available Capacity Mounted\nfs 900000000 1 600000000 1% /data"
        if "psql" in command:
            return "PostgreSQL 16 test"
        if "info" in command:
            return json.dumps({"Architecture": "aarch64", "MemTotal": 24 << 30, "NCPU": 8})
        if "ps" in command:
            return "worker-id"
        if "exec" in command:
            return json.dumps(runtime)
        if "image" in command:
            return json.dumps([{"Architecture": "arm64", "Os": "linux", "Id": "image"}])
        return json.dumps([{"Image": "image", "HostConfig": {"Memory": 16 << 30}}])
    monkeypatch.setattr(orchestration, "output", output)
    monkeypatch.setattr(orchestration.shutil, "disk_usage", lambda _: SimpleNamespace(free=500 << 30))
    monkeypatch.setattr(appliance_acceptance, "request", lambda *a, **k: {"status": "ready"})
    args = SimpleNamespace(context="default", project="tracex-benchmark-test", install=tmp_path, counts=None, source=None, base="http://localhost:8000")
    assert orchestration.preflight(args)["status"] == "ADMITTED_NOT_RUN"
    args.estimate_transactions = 1_000_000
    estimated = orchestration.preflight(args)
    assert estimated["admission_counts"] == {"transactions": 1_000_000, "inputs": 1_500_000, "outputs": 2_700_000}
    assert estimated["counts_scope"].startswith("ESTIMATED METADATA ONLY")
    runtime["candidate_configured"] = True
    runtime["candidate_admission"] = {"status": "failed", "error": "test integrity mismatch"}
    blocked = orchestration.preflight(args)
    assert blocked["status"] == "BLOCKED" and any("candidate integrity" in error for error in blocked["errors"])
    runtime["candidate_configured"] = False
    runtime["resource_plan"]["memory_budget_bytes"] = 5 << 30
    report = orchestration.preflight(args)
    assert report["status"] == "BLOCKED" and any("memory admission" in error for error in report["errors"])
    assert report["large_run"] == "NOT RUN"


def test_init_is_exclusive_does_not_launch_jobs_and_masks_secrets(tmp_path, capsys):
    install = tmp_path / "install"
    args = ["init", "--install", str(install), "--image", "tracex:native", "--memory-mb", "12000", "--workers", "2"]
    assert orchestration.main(args) == 0
    environment = (install / ".env").read_text()
    assert "TRACEX_ML_FINDINGS=1" in environment and "OMP_NUM_THREADS=1" in environment
    assert "TRACEX_UI_INTERNAL=false" in environment
    secret = next(line.split("=", 1)[1] for line in environment.splitlines() if line.startswith("TRACEX_SECRET_KEY="))
    assert secret not in capsys.readouterr().out
    assert (install / ".env").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        orchestration.main(args)


def test_mac_resource_fallback_reports_unknown_not_zero(monkeypatch):
    import sys

    from app import telemetry
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(telemetry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(telemetry, "command_text", lambda command: "34359738368" if command[0] == "sysctl" else None)
    result = telemetry.memory_info()
    assert result["physical_bytes"] == 32 << 30 and result["available_bytes"] is None
    assert telemetry.process_rss_bytes(1) is None
    monkeypatch.setattr(telemetry, "command_text", lambda command: "Mach Virtual Memory Statistics: (page size of 16384 bytes)" if command[0] == "vm_stat" else "34359738368")
    assert telemetry.memory_info()["available_bytes"] is None


def test_independent_count_manifest_and_duplicate_reconciliation_on_64_rows(tmp_path):
    from app.ml.candidate import sha
    from scripts.quality_matrix import independent_fixture
    dataset = tmp_path / "small-count"
    independent_fixture(dataset, 64, 19, "count")
    source = dataset / "ingestion_rows.ndjson"
    first = source.read_text().splitlines()[0]
    with source.open("a") as stream:
        stream.write(first + "\n")
    (dataset / "dataset_manifest.json").write_text(json.dumps({"files": {source.name: {"sha256": sha(source)}}, "counts": {"transactions": 64}}))
    result = tmp_path / "counts.json"
    assert orchestration.main(["count", "--source", str(source), "--output", str(result)]) == 0
    counts = json.loads(result.read_text())
    provenance = json.loads(result.with_suffix(".provenance.json").read_text())
    assert counts["transactions"] == 64 and counts["quarantine"] == 1 and counts["inputs"] == 3
    assert provenance["source_rows"] == 65 and provenance["source_sha256"] == sha(source)


def test_runbook_small_fixture_count_uses_its_real_manifest(tmp_path):
    from scripts.quality_matrix import independent_fixture
    directory = tmp_path / "small"
    independent_fixture(directory, 64, 19, "count")
    output = tmp_path / "counts.json"
    assert orchestration.main(["count", "--source", str(directory / "ingestion_rows.ndjson"), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["transactions"] == 64
    assert json.loads(output.with_suffix(".provenance.json").read_text())["manifest_kind"] == "quality_manifest.json"


@pytest.mark.parametrize("command", ["init", "build", "start", "configure", "candidate", "preflight", "screen", "accept", "profile", "generate", "count"])
def test_command_help_does_not_execute_workloads(command):
    with pytest.raises(SystemExit) as result:
        orchestration.main([command, "--help"])
    assert result.value.code == 0
