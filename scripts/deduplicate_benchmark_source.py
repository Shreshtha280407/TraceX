"""Retire only an identical disposable generator copy, retaining both paths.

The immutable benchmark raw upload and generated NDJSON become hard links to
one verified inode. Never use for real case data; no bytes are rewritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def deduplicate(run, source):
    if run.is_symlink() or source.is_symlink() or source.parent.is_symlink():
        raise ValueError("Benchmark/source symlinks are forbidden")
    run, source = run.resolve(strict=True), source.resolve(strict=True)
    if run.parent != REPO / "var/scale-run" or source.parent.parent != REPO / "datasets":
        raise ValueError("Only local benchmark and generated dataset paths are allowed")
    if source.name != "ingestion_rows.ndjson" or not source.parent.name.startswith("review_"):
        raise ValueError("Only disposable review NDJSON is eligible")
    manifest_path = source.parent / "dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("seed", "").startswith("tracex-review-"):
        raise ValueError("Not an explicitly generated review dataset")
    result = json.loads((run / "result.json").read_text())
    if result.get("status") not in {"pass", "failed"}:
        raise ValueError("Benchmark must have a retained terminal result")
    database = sqlite3.connect(f"file:{run / 'control.db'}?mode=ro", uri=True)
    try:
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Benchmark database integrity failed")
        cases = database.execute("SELECT id,name,synthetic FROM cases").fetchall()
        if len(cases) != 1 or cases[0][0] != result["case_id"] or not cases[0][1].startswith("scale ") or not cases[0][2]:
            raise ValueError("Not an exclusively synthetic benchmark")
        jobs = database.execute("SELECT id,state FROM import_jobs").fetchall()
        if len(jobs) != 1 or jobs[0][0] != result["job_id"] or jobs[0][1] not in {"completed", "failed"}:
            raise ValueError("Live benchmark cannot be deduplicated")
        sources = database.execute("SELECT storage_relative_path,sha256,byte_size FROM evidence_sources").fetchall()
        if len(sources) != 1:
            raise ValueError("Expected exactly one benchmark upload")
        relative, expected, size = sources[0]
    finally:
        database.close()
    raw = run / "evidence" / relative
    if raw.is_symlink() or not raw.resolve(strict=True).is_relative_to(run / "evidence"):
        raise ValueError("Unsafe immutable upload path")
    expected_manifest = manifest["files"][source.name]
    if expected != expected_manifest["sha256"] or source.stat().st_size != size or raw.stat().st_size != size:
        raise ValueError("Source manifest/size mismatch")
    for path in (source, raw):
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError("Source integrity mismatch; nothing removed")
    if source.samefile(raw):
        return {"status": "already_shared", "bytes": size}
    report = {"status": "verified_identical", "source": str(source), "immutable_raw": str(raw),
              "sha256": expected, "bytes": size, "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "scope": "only duplicate disposable generator inode retired; both byte-identical paths retained",
              "disk_before": shutil.disk_usage(source)._asdict()}
    archive = source.parent / "deduplicated-source-20261003.json"
    with archive.open("x") as handle:
        json.dump(report, handle, indent=2)
    with tempfile.TemporaryDirectory(prefix="verified-source-link-", dir=source.parent) as directory:
        replacement = Path(directory) / source.name
        os.link(raw, replacement)
        os.replace(replacement, source)
    assert source.samefile(raw)
    with archive.with_suffix(".completed.json").open("x") as handle:
        json.dump({"status": "pass", "sha256": expected, "source_bytes_retained": True,
                   "disk_after": shutil.disk_usage(source)._asdict()}, handle, indent=2)
    return {"status": "pass", "retired_duplicate_bytes": size, "source_bytes_retained": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(deduplicate(args.run, args.source)))


if __name__ == "__main__":
    main()
