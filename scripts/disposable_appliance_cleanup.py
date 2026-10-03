"""Archive and retire one verified synthetic scale case in the review appliance.

Run inside the isolated review container with its saved acceptance result.
No deletion unless --delete; never accepts real cases, active jobs, extra
imports, analyst reviews or risk seeds. The result/logs stay on the host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, make_engine
from app.models import (
    AnalyticsRevision,
    AnalyticsSnapshot,
    Case,
    EvidenceSource,
    FeatureStoreRecord,
    FragmentReceipt,
    GraphSnapshot,
    ImportJob,
    ReviewDecisionRecord,
    RiskSeed,
    User,
)


def archive(session, result, *, name, root):
    if result.get("status") not in {"pass", "failed"}:
        raise ValueError("Expected preserved terminal benchmark result")
    case_id = str(uuid.UUID(result["case_id"]))
    case = session.get(Case, case_id)
    if case is None or not case.synthetic or case.name != "scale offline " + name:
        raise ValueError("Not the exact synthetic offline benchmark case")
    owner = session.get(User, case.created_by)
    if owner is None or not owner.external_subject.startswith("offline-review-"):
        raise ValueError("Not a disposable benchmark account")
    jobs = list(session.scalars(select(ImportJob).where(ImportJob.case_id == case_id)))
    sources = list(session.scalars(select(EvidenceSource).where(EvidenceSource.case_id == case_id)))
    if len(jobs) != 1 or jobs[0].id != result["job_id"] or jobs[0].state not in {"failed", "completed"}:
        raise ValueError("Active/recoverable/extra jobs are not disposable")
    if len(sources) != 1 or sources[0].sha256 != result.get("source_sha256"):
        raise ValueError("Source does not match the saved benchmark")
    for model in (ReviewDecisionRecord, RiskSeed):
        if session.scalar(select(func.count()).select_from(model).where(model.case_id == case_id)):
            raise ValueError("Analyst decisions/seeds must never be retired as disposable")
    directory = root / case_id
    if directory.is_symlink() or directory.resolve().parent != root.resolve() or not directory.is_dir():
        raise ValueError("Unsafe/missing exact case vault")
    expected = {}
    for model in (EvidenceSource, FragmentReceipt, GraphSnapshot, FeatureStoreRecord, AnalyticsSnapshot, AnalyticsRevision):
        for path, checksum in session.execute(select(model.storage_relative_path, model.sha256).where(model.case_id == case_id)):
            expected[path] = checksum
    artifacts = []
    for target in sorted(directory.rglob("*")):
        if target.is_symlink():
            raise ValueError("Artifact symlinks are not eligible")
        if target.is_file():
            with target.open("rb") as handle:
                checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            artifacts.append({"path": str(target.relative_to(root)), "bytes": target.stat().st_size, "sha256": checksum})
    observed = {item["path"]: item["sha256"] for item in artifacts}
    if any(observed.get(path) != checksum for path, checksum in expected.items()):
        raise ValueError("Receipted artifact integrity mismatch; nothing deleted")
    counts = {table.name: session.scalar(select(func.count()).select_from(table).where(table.c.case_id == case_id))
              for table in Base.metadata.sorted_tables if "case_id" in table.c}
    return {"case_id": case_id, "name": case.name, "artifacts": artifacts,
            "receipt_hashes_verified": len(expected), "database_counts": counts,
            "vault_logical_bytes": sum(item["bytes"] for item in artifacts),
            "benchmark_status": result["status"], "disk_before": shutil.disk_usage(root)._asdict(),
            "retained": "host acceptance result, logs, resources, source/dataset manifests and this checksum archive",
            "recoverable_after_deletion": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    root = settings.evidence_root.resolve(strict=True)
    if root != Path("/data/evidence") or not settings.database_url.startswith("postgresql"):
        raise ValueError("Only the configured copied review appliance may run this command")
    result = json.loads(args.result.read_text())
    engine = make_engine()
    try:
        with Session(engine) as session:
            report = archive(session, result, name=args.name, root=root)
            report["result_sha256"] = hashlib.sha256(args.result.read_bytes()).hexdigest()
            report["deletion_requested"] = args.delete
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as handle:
                json.dump(report, handle, indent=2)
            if args.delete:
                # Cascade only this case. Roll back if an unexpected FK leaves
                # case-scoped rows behind; never delete unrelated rows manually.
                session.execute(Case.__table__.delete().where(Case.id == report["case_id"]))
                for table in Base.metadata.sorted_tables:
                    if "case_id" in table.c and session.scalar(select(func.count()).select_from(table).where(table.c.case_id == report["case_id"])):
                        raise ValueError("Unexpected incomplete case cascade; vault preserved")
                session.commit()
                shutil.rmtree(root / report["case_id"])
                with args.output.with_suffix(".deleted.json").open("x") as handle:
                    json.dump({"case_id": report["case_id"], "disk_after": shutil.disk_usage(root)._asdict(),
                               "recoverable": False}, handle, indent=2)
            print(json.dumps({"case_id": report["case_id"], "vault_bytes": report["vault_logical_bytes"], "deleted": args.delete}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
