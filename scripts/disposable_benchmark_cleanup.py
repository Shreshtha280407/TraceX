"""Archive verified disposable test artifacts before explicitly authorized deletion.

Never touches real case vaults or an arbitrary directory. No deletion by default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "var/scale-run"


def archive(run, *, delete=False, include_failed=False):
    if run.is_symlink():
        raise ValueError("Benchmark directory symlinks are not allowed")
    run = run.resolve(strict=True)
    if run.parent != ROOT.resolve() or run.is_symlink():
        raise ValueError("Only an explicit immediate child of var/scale-run is allowed")
    result = json.loads((run / "result.json").read_text())
    if result.get("status") != "pass" and not (include_failed and result.get("status") == "failed"):
        raise ValueError("Only successful terminal benchmarks are eligible for automated cleanup")
    report = {"run": str(run), "result_sha256": hashlib.sha256((run / "result.json").read_bytes()).hexdigest(),
              "irrecoverable": True, "kept": "results, stage/resource metrics, profiles, logs, fingerprints and manifests",
              "artifacts": []}
    report["benchmark_status"] = result["status"]
    report["disk_before"] = shutil.disk_usage(run)._asdict()
    db = sqlite3.connect(f"file:{run / 'control.db'}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        cases = db.execute("SELECT name,synthetic FROM cases").fetchall()
        if not cases or any(not name.startswith("scale ") or not synthetic for name, synthetic in cases):
            raise ValueError("Not an exclusively synthetic benchmark database")
        jobs = db.execute("SELECT state FROM import_jobs").fetchall()
        if not jobs or any(state not in {"failed", "completed"} for (state,) in jobs):
            raise ValueError("Benchmark jobs must be terminal; active/recoverable leases are not disposable")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        report["database_counts"] = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in tables}
        report["receipts"] = [dict(zip(("type", "path", "sha256", "records"), row, strict=True)) for row in db.execute(
            "SELECT record_type,storage_relative_path,sha256,record_count FROM fragment_receipts")]
        expected = {"evidence/" + path: checksum for table in
                    ("evidence_sources", "fragment_receipts", "graph_snapshots", "feature_stores", "analytics_snapshots", "analytics_revisions")
                    if table in tables for path, checksum in db.execute(f'SELECT storage_relative_path,sha256 FROM "{table}"')}
    finally:
        db.close()
    for target in [run / "control.db", run / "control.db-wal", run / "control.db-shm", *sorted((run / "evidence").rglob("*"))]:
        if not target.is_file():
            continue
        if target.is_symlink() or not target.resolve().is_relative_to(run):
            raise ValueError("Artifact symlink/path escape")
        with target.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        report["artifacts"].append({"path": str(target.relative_to(run)), "bytes": target.stat().st_size,
                                    "hardlink_count": target.stat().st_nlink, "sha256": digest})
    observed = {item["path"]: item["sha256"] for item in report["artifacts"]}
    for path, checksum in expected.items():
        if observed.get(path) != checksum:
            raise ValueError(f"Receipted artifact integrity mismatch: {path}; nothing deleted")
    report["receipt_hashes_verified"] = len(expected)
    report["removed_bytes"] = sum(item["bytes"] for item in report["artifacts"])
    report["deletion_requested"] = delete
    with (run / "disposable-artifact-archive.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    if delete:
        shutil.rmtree(run / "evidence")
        for name in ("control.db", "control.db-wal", "control.db-shm"):
            (run / name).unlink(missing_ok=True)
        (run / "disposable-data-deleted.json").write_text(json.dumps({"removed_logical_bytes": report["removed_bytes"],
            "disk_after": shutil.disk_usage(run)._asdict(), "recoverable": False,
            "note": "hardlinks or concurrent filesystem work can make physical free-space change differ"}))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--delete", action="store_true", help="Requires user authorization; disposable data is unrecoverable")
    parser.add_argument("--include-failed", action="store_true", help="Explicitly permit terminal synthetic failures; preserves failure/partial-output evidence")
    args = parser.parse_args()
    result = archive(args.run, delete=args.delete, include_failed=args.include_failed)
    print(json.dumps({"run": result["run"], "bytes": result["removed_bytes"], "deleted": args.delete}))


if __name__ == "__main__":
    main()
