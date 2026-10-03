"""Full published ML/pin comparison of two passing synthetic appliance imports.

Run inside the copied appliance (or via python stdin). Reads only PostgreSQL;
does not train, retry, modify cases, reinterpret pins or omit scoring fields.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import make_engine
from app.models import Case, EvidenceSource, FindingConfidence, FindingRecord, GraphSnapshot, ImportJob, Snapshot, User


def normalize(value, identities):
    if isinstance(value, dict):
        return {key: normalize(item, identities) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(item, identities) for item in value]
    if isinstance(value, str):
        for identity, marker in identities.items():
            value = value.replace(identity, marker)
        return value
    return value.isoformat() if hasattr(value, "isoformat") else value


def digest(documents):
    return hashlib.sha256("\n".join(json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
                                    for item in documents).encode()).hexdigest()


def fingerprint(session, result):
    if result.get("status") != "pass" or result.get("acceptance_errors"):
        raise ValueError("Both complete benchmarks must pass")
    case, job = session.get(Case, result["case_id"]), session.get(ImportJob, result["job_id"])
    if not case or not case.synthetic or not case.name.startswith("scale offline ") or not job or job.state != "completed" or job.case_id != case.id:
        raise ValueError("Only terminal synthetic appliance benchmarks are eligible")
    owner = session.get(User, case.created_by)
    source, snapshot = session.get(EvidenceSource, job.source_id), session.get(Snapshot, job.snapshot_id)
    if not source or not snapshot or source.case_id != case.id or snapshot.case_id != case.id or snapshot.job_id != job.id:
        raise ValueError("Source/snapshot must belong to the exact benchmark case/job")
    graph = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == snapshot.id))
    if not owner or not owner.external_subject.startswith("offline-review-") or source.sha256 != result["source_sha256"] or not graph:
        raise ValueError("Benchmark owner/source/graph linkage mismatch")
    stage = next(row for row in result["job"]["analysis"]["stages"] if row["name"] == "ml_scoring")
    details = stage["details"]
    if details.get("release_id") != "anomaly-stack-v2" or not details.get("model_run_id"):
        raise ValueError("Expected complete retained v2 scoring identity")
    identities = {case.id: "<CASE>", job.id: "<JOB>", source.id: "<SOURCE>", snapshot.id: "<SNAPSHOT>",
                  graph.id: "<GRAPH>", details["model_run_id"]: "<MODEL_RUN>"}
    fields, pins = [], []
    query = select(FindingRecord.__table__, FindingConfidence.provenance.label("pinned_confidence")).outerjoin(
        FindingConfidence, FindingConfidence.finding_id == FindingRecord.id).where(
        FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == "anomaly-stack-v2").order_by(
        FindingRecord.entity_ref, FindingRecord.rule_id).execution_options(yield_per=128)
    for row in session.execute(query).mappings():
        original = row["feature_vector"]
        original_hash = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if original_hash != row["feature_vector_hash"] or original.get("model_run_id") != details["model_run_id"] or row["pinned_confidence"] is None:
            raise ValueError("Invalid original feature hash/model link or missing write-once pin")
        document = normalize({key: value for key, value in row.items() if key not in
                              {"id", "created_at", "updated_at", "pinned_confidence"}}, identities)
        document["feature_vector_hash"] = hashlib.sha256(json.dumps(document["feature_vector"], sort_keys=True,
                                                                   separators=(",", ":")).encode()).hexdigest()
        fields.append(document)
        pins.append(normalize([row["entity_ref"], row["rule_id"], row["pinned_confidence"]], identities))
    if len(fields) != details["written"] or len(fields) != details["threshold_flagged"]:
        raise ValueError("Published count differs from verified ML stage")
    return {"findings": len(fields), "sha256": digest(fields), "confidence_pins_sha256": digest(pins),
            "scored_transactions": details["scored_transactions"], "release_id": details["release_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if settings.evidence_root != Path("/data/evidence") or not settings.database_url.startswith("postgresql"):
        raise ValueError("Only the isolated review appliance is eligible")
    report = {"pass": False, "scope": "full published ML fields and pinned confidence; normalize known run IDs only; not full 1M graph/features parity",
              "original_feature_hashes_and_model_links_verified": False}
    engine = make_engine()
    try:
        inputs = [json.loads(path.read_text()) for path in (args.baseline, args.candidate)]
        if inputs[0]["source_sha256"] != inputs[1]["source_sha256"]:
            raise ValueError("Source inputs must be identical")
        report["source_sha256"] = inputs[0]["source_sha256"]
        report["input_report_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.baseline, args.candidate)}
        with Session(engine) as session:
            report["reports"] = [fingerprint(session, result) for result in inputs]
        report["original_feature_hashes_and_model_links_verified"] = True
        report["pass"] = report["reports"][0] == report["reports"][1]
    except Exception as error:  # noqa: BLE001 - preserve a failed comparison, never weaken its scope
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        engine.dispose()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump(report, handle, indent=2)
        print(json.dumps(report))
    return int(not report["pass"])


if __name__ == "__main__":
    raise SystemExit(main())
