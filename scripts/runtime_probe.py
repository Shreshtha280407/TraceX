"""Run INSIDE the Linux worker, including Docker Desktop's VM, not on macOS PID aliases."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
from pathlib import Path

from app.resources import available_memory_bytes, current_plan, global_stage_estimates
from app.telemetry import host_metrics, process_rss_bytes


def probe(counts=None, *, metrics_only=False):
    cgroup = {}
    for name in ("memory.max", "memory.current", "memory.peak", "cpu.max"):
        try:
            cgroup[name] = Path("/sys/fs/cgroup", name).read_text().strip()
        except OSError:
            cgroup[name] = None
    result = {"pid1_rss_bytes": process_rss_bytes(1), "pid1_tree_rss_bytes": process_rss_bytes(1, tree=True),
              "cgroup": cgroup, "effective_available_bytes": available_memory_bytes(),
              "scope": "actual worker Linux environment; child RSS can double-count shared pages"}
    if metrics_only:
        return result
    from app.config import settings
    from app.ml.findings import release_identity
    plan = current_plan()
    geoip = Path(os.environ.get("TRACEX_GEOIP_DIR", "/opt/tracex/geoip"))
    from scripts.prepare_geoip_cache import verify_cache
    try:
        geoip_integrity = {"status": "verified", **verify_cache(geoip / "compiled")}
    except (OSError, ValueError, KeyError) as error:
        geoip_integrity = {"status": "failed", "error": str(error)}
    result.update(host=host_metrics(), resource_plan=plan.as_dict(), unsupervised_fallback_scorer=release_identity(),
        default_selection_policy="auto_eligible: exact approved data domain and measured queue gates, otherwise v2",
        requested_workers=os.getenv("TRACEX_WORKERS"), effective_workers=plan.worker_processes(records=3_050_000),
        ml_enabled=settings.ml_findings_enabled,
        geoip_integrity=geoip_integrity,
        candidate_configured=bool(settings.candidate_directory), candidate_manifest_sha256=settings.candidate_manifest_sha256,
        evidence_filesystem=shutil.disk_usage(settings.evidence_root)._asdict(),
        versions={name: importlib.metadata.version(name) for name in ("duckdb", "pyarrow", "numpy", "scikit-learn", "scipy", "SQLAlchemy", "psycopg")},
        code_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest() for root in (Path("app"), Path("workers")) for path in sorted(root.rglob("*.py"))},
        native_threads={key: os.getenv(key) for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")},
        geoip_files={str(path.relative_to(geoip)): hashlib.file_digest(path.open("rb"), "sha256").hexdigest() for path in geoip.rglob("*") if path.is_file()})
    if counts:
        extra = 0
        if settings.candidate_directory:
            try:
                path = settings.candidate_directory / "manifest.json"
                if path.stat().st_size > 1 << 20:
                    raise ValueError("candidate manifest exceeds 1 MiB")
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != settings.candidate_manifest_sha256:
                    raise ValueError("candidate manifest pin mismatch")
                manifest = json.loads(content)
                from app.ml.candidate import contract_identity, library_version
                columns, feature_hash = contract_identity(manifest.get("feature_contract"))
                if manifest.get("feature_sha256") != feature_hash or manifest.get("columns") != list(columns):
                    raise ValueError("candidate feature contract mismatch")
                for name, version in manifest["versions"].items():
                    if library_version(name) != version:
                        raise ValueError(f"candidate library version mismatch: {name}")
                payload = settings.candidate_directory / "weights.joblib"
                if payload.stat().st_size > 128 << 20:
                    raise ValueError("candidate payload exceeds 128 MiB")
                with payload.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["payload_sha256"]:
                        raise ValueError("candidate weight integrity mismatch")
                expanded = manifest.get("expanded_artifact_bytes")
                if not isinstance(expanded, int) or isinstance(expanded, bool) or not 0 < expanded <= 256 << 20:
                    raise ValueError("candidate expanded working set is invalid")
                from app.ml.recipient_history import working_set_bytes
                extra = expanded * 3 + working_set_bytes(counts.get("transactions", 0), counts.get("outputs", 0))
                result["candidate_admission"] = {"status": "verified_metadata", "extra_bytes": extra,
                    "release_id": manifest["release_id"], "scope": "configured candidate joint weights/recipient workspace; not a claim of case eligibility"}
            except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError) as error:
                result["candidate_admission"] = {"status": "failed", "error": str(error)}
        result["estimates"] = global_stage_estimates(counts, native_bytes=max(256, plan.memory_budget_bytes // (4 << 20)) * (1 << 20), candidate_extra_bytes=extra)
        result["new_case_scorer_policy"] = "auto_eligible; declared approved domain and measured gates, otherwise v2 fallback"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-only", action="store_true")
    parser.add_argument("--counts-json", help="metadata JSON, never allocates transactions")
    args = parser.parse_args(argv)
    print(json.dumps(probe(json.loads(args.counts_json) if args.counts_json else None, metrics_only=args.metrics_only)))


if __name__ == "__main__":
    main()
