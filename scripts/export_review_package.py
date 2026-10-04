"""Compact sanitized underlying results, not just an inventory. Never copies case data."""
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SENSITIVE = re.compile(r"token|password|secret|credential|authorization|cookie|source_refs|record|entity_ref|case_id|job_id|email", re.IGNORECASE)
KEEP = {"status", "scope", "acceptance_errors", "error", "exit_code", "total_seconds", "elapsed_seconds",
        "upload_seconds", "first_provisional_activity_seconds", "clock_contract", "source_sha256", "expected_counts",
        "count_provenance", "container_runtime", "containers", "postgres_version", "resource_peaks", "memory_measurement",
        "container_cgroup_peak_bytes", "cgroup_peak_scope", "scratch_measurement", "final_findings_retrieval",
        "models", "results", "procedure", "model", "dataset", "truth_sha256", "split_overlap", "promotion",
        "host", "docker_vm", "worker", "image", "container_memory_limit", "disk_required_bytes", "recommendations",
        "warnings", "errors", "large_run", "network_scope", "run_id", "test_summary", "checks", "passed", "failed",
        "schema", "quality_gates_passed", "gate_details", "metric_coverage", "queue_comparison", "queue_coverage",
        "baseline", "candidate", "comparison", "reproduction", "protocol", "feature_contract", "selected_safe_workers", "preflight", "bottleneck", "first_useful_output_seconds", "first_useful_output_scope", "matrix_coverage", "worst_case", "gates", "strata", "protocol_sha256", "reserved_finals", "selection", "grid", "generalization", "targets", "inference_adaptation", "manifest_sha256", "validation_report_sha256", "reason", "release_id"}


def sanitize(value):
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items() if not SENSITIVE.search(key)}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", value, flags=re.IGNORECASE)
        value = re.sub(r"(password|secret|token|authorization)([=:]\s*)[^\s,;]+", r"\1\2[REDACTED]", value, flags=re.IGNORECASE)
        value = re.sub(r"https?://[^/@\s]+:[^/@\s]+@", "https://[REDACTED]@", value)
        value = re.sub(r"/home/[^/\s]+|/Users/[^/\s]+", "[HOME]", value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, action="append", default=[])
    parser.add_argument("--junit", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    inventory = []
    for i, path in enumerate(args.result):
        if path.stat().st_size > 8 << 20:
            raise ValueError("result exceeds compact 8 MiB input limit; provide the summary, not raw evidence")
        source = json.loads(path.read_text())
        selected = {key: sanitize(value) for key, value in source.items() if key in KEEP}
        if "job" in source:
            job = source["job"]
            selected["pipeline"] = sanitize({key: job.get(key) for key in ("state", "rows_seen", "rows_accepted", "rows_quarantined", "analysis")})
        if "inspection" in source:
            selected["counts"] = sanitize(source["inspection"].get("canonical_counts"))
        environment = source.get("host_harness_environment", source.get("environment", {}))
        selected["code_identity"] = sanitize({key: environment.get(key) for key in ("commit", "diff_sha256", "untracked_sha256", "locks", "runtime_source_sha256", "resources", "versions")})
        selected["dirty_tree"] = bool(environment["git_status"]) if "git_status" in environment else None
        destination = args.output / f"result-{i:02d}.json"
        destination.write_text(json.dumps(selected, indent=2))
        inventory.append({"file": destination.name, "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    for i, path in enumerate(args.junit):
        tree = ET.parse(path)
        suites = list(tree.getroot().iter("testsuite"))
        summary = [{key: suite.attrib.get(key) for key in ("name", "tests", "errors", "failures", "skipped", "time")} for suite in suites]
        destination = args.output / f"tests-{i:02d}.json"
        destination.write_text(json.dumps(summary, indent=2))
        inventory.append({"file": destination.name, "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    for item in inventory:
        item["sha256"] = hashlib.sha256((args.output / item["file"]).read_bytes()).hexdigest()
    (args.output / "inventory.json").write_text(json.dumps(inventory, indent=2))
    (args.output / "README.md").write_text("# TraceX compact evidence\n\nUnderlying sanitized results are included. Review files manually before Git publication. Raw sources, credentials and case exports are intentionally excluded. Missing MacBook/quality results are NOT RUN, not passes. See docs/macbook_runbook.md for reproduction.\n")
    print(str(args.output))


if __name__ == "__main__":
    main()
