"""Batch ingestion with deterministic normalization, immutable fragments, and durable receipts."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from itertools import islice

import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.engine.adapters import ParsedRow, SourceParseError, count_records, rows_for_source
from app.engine.canonical import NormalizationError, normalize_row
from app.engine.catalogue import publish_batch
from app.engine.findings import materialize_findings
from app.engine.graph.builder import build_graph_snapshot
from app.events import append_event
from app.jobs.service import utcnow
from app.models import EvidenceSource, FragmentReceipt, GraphSnapshot, ImportCheckpoint, ImportJob, Snapshot
from app.storage.raw import resolve_source

logger = logging.getLogger(__name__)

PARSER_REVISION = "phase2-ingestion-v1"


def _chunks(rows: Iterator[ParsedRow], size: int) -> Iterator[list[ParsedRow]]:
    while batch := list(islice(rows, size)):
        yield batch


#: A review budget outside this fraction of the snapshot is not a safe
#: configuration: at or below 0 there is no queue at all, and above half the
#: snapshot "triage" has stopped meaning anything. This is a runtime safety rail
#: on `TRACEX_ML_REVIEW_BUDGET`, independent of whatever numeric value `float()`
#: is willing to parse.
ML_BUDGET_SAFE_RANGE = (0.0, 0.5)


def _materialize_ml_findings(session, *, settings, snapshot, graph) -> dict[str, object]:
    """Run the anomaly stack on the completed snapshot, if it is available.

    Deliberately non-fatal. The stack lives behind the optional `ml` extra, and an
    ingestion that has already committed correct evidence and correct
    deterministic findings must not be failed because a ranking could not be
    produced. Every outcome is recorded on the completion event so a silent skip
    is still visible.

    Only the unsupervised layers run here; the supervised comparator needs analyst
    review decisions and must never write into a case on generator truth.
    """
    if not getattr(settings, "ml_findings_enabled", True):
        return {"written": 0, "status": "disabled"}

    budget = getattr(settings, "ml_review_budget", 0.01)
    low, high = ML_BUDGET_SAFE_RANGE
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not (low < budget <= high):
        # A misconfigured budget must not silently score against a nonsensical
        # threshold (0, negative, or "flag half the snapshot"); it must not raise
        # either, since raising here would look like a scoring failure rather
        # than a configuration one. `invalid_budget` is a distinct, visible
        # status neither `disabled` nor `error:*` would honestly describe.
        logger.warning(
            "TRACEX_ML_REVIEW_BUDGET=%r for snapshot %s is outside the safe range %s; skipping ML findings",
            budget, snapshot.id, ML_BUDGET_SAFE_RANGE,
        )
        return {"written": 0, "status": "invalid_budget"}

    try:
        from app.ml.findings import materialize_ml_findings
    except ImportError:
        # scikit-learn/numpy absent: Phase 4 findings still stand on their own.
        return {"written": 0, "status": "unavailable"}
    try:
        result = materialize_ml_findings(
            session, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph, budget=float(budget),
        )
    except Exception as error:  # noqa: BLE001 - a ranking failure must not lose the import
        logger.warning("anomaly stack did not run for snapshot %s: %s", snapshot.id, error)
        return {"written": 0, "status": f"error:{type(error).__name__}"}
    return {
        "written": result.written,
        "status": "written" if result.written else "no_rows_flagged",
        "model_run_id": result.model_run_id,
        "release_id": result.release_id,
    }


def _snapshot_for_job(session: Session, job: ImportJob, source: EvidenceSource) -> Snapshot:
    existing = session.scalar(select(Snapshot).where(Snapshot.job_id == job.id))
    if existing:
        return existing
    # The case lock also serializes snapshot version allocation on PostgreSQL.
    from app.models import Case

    session.scalar(select(Case).where(Case.id == job.case_id).with_for_update())
    version = (session.scalar(select(func.max(Snapshot.version)).where(Snapshot.case_id == job.case_id)) or 0) + 1
    snapshot = Snapshot(
        case_id=job.case_id,
        job_id=job.id,
        version=version,
        state="provisional",
        provisional=True,
        source_manifest_hash=source.sha256,
    )
    session.add(snapshot)
    session.flush()
    job.snapshot_id = snapshot.id
    return snapshot


def _quarantine(*, case_id: str, source_id: str, source_sha256: str, row: ParsedRow, reason: str) -> dict:
    return {
        "case_id": case_id,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "logical_record": row.logical_record,
        "source_locator": row.locator,
        "reason": reason[:1000],
        "raw_record_json": json.dumps(row.value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)[:65536],
        "parser_revision": PARSER_REVISION,
        "source_refs": [
            {
                "evidence_id": source_id,
                "source_sha256": source_sha256,
                "locator_type": row.locator_type,
                "locator": row.locator,
                "byte_start": None,
                "byte_end": None,
            }
        ],
    }


def _last_checkpoint(session: Session, job_id: str) -> int:
    return (
        session.scalar(select(func.max(ImportCheckpoint.logical_record)).where(ImportCheckpoint.job_id == job_id)) or 0
    )


def _committed_txids(session: Session, *, evidence_root, job_id: str) -> set[str]:
    """Rebuild duplicate state on resume from receipt-approved, never-orphan fragments."""
    txids: set[str] = set()
    receipts = session.scalars(
        select(FragmentReceipt).where(FragmentReceipt.job_id == job_id, FragmentReceipt.record_type == "transactions")
    )
    root = evidence_root.resolve()
    for receipt in receipts:
        path = (root / receipt.storage_relative_path).resolve()
        if root not in path.parents:
            raise RuntimeError("fragment receipt escaped evidence root")
        for value in pq.read_table(path, columns=["txid"]).column("txid").to_pylist():
            if value:
                txids.add(value)
    return txids


def _commit_batch(
    session: Session,
    *,
    settings: Settings,
    job: ImportJob,
    source: EvidenceSource,
    batch_number: int,
    rows: list[ParsedRow],
    facts: dict[str, list[dict]],
    accepted: int,
    quarantined: int,
) -> None:
    artifacts = publish_batch(
        evidence_root=settings.evidence_root,
        case_id=job.case_id,
        source_sha256=source.sha256,
        source_id=source.id,
        logical_batch=batch_number,
        facts=facts,
    )
    snapshot = _snapshot_for_job(session, job, source)
    for artifact in artifacts:
        receipt = session.scalar(
            select(FragmentReceipt).where(
                FragmentReceipt.job_id == job.id,
                FragmentReceipt.logical_batch == batch_number,
                FragmentReceipt.record_type == artifact.record_type,
            )
        )
        if receipt is None:
            session.add(
                FragmentReceipt(
                    case_id=job.case_id,
                    job_id=job.id,
                    snapshot_id=snapshot.id,
                    source_id=source.id,
                    logical_batch=batch_number,
                    record_type=artifact.record_type,
                    record_count=artifact.record_count,
                    sha256=artifact.sha256,
                    byte_size=artifact.byte_size,
                    storage_relative_path=artifact.storage_relative_path,
                    parser_revision=PARSER_REVISION,
                    source_record_start=rows[0].logical_record,
                    source_record_end=rows[-1].logical_record,
                )
            )
    job.state = "checkpointed"
    job.stage = "ingesting"
    job.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
    job.rows_seen += len(rows)
    job.rows_accepted += accepted
    job.rows_quarantined += quarantined
    # Reader byte offsets are not reliable across quoted CSV fields and streamed JSON/XML.
    # Report only committed exact bytes: zero until the full immutable source is consumed.
    job.bytes_read = 0
    session.add(
        ImportCheckpoint(
            job_id=job.id,
            logical_record=rows[-1].logical_record,
            bytes_read=job.bytes_read,
            rows_seen=job.rows_seen,
            rows_accepted=job.rows_accepted,
            rows_quarantined=job.rows_quarantined,
            parser_revision=PARSER_REVISION,
        )
    )
    append_event(
        session,
        case_id=job.case_id,
        event_type="batch.committed",
        stage="ingesting",
        job=job,
        payload={
            "logical_batch": batch_number,
            "rows_seen": job.rows_seen,
            "rows_valid": job.rows_accepted,
            "rows_quarantined": job.rows_quarantined,
            "provisional": True,
            "fragments": len(artifacts),
        },
    )
    session.commit()


def ingest_source(session: Session, *, settings: Settings, job: ImportJob, source: EvidenceSource) -> None:
    """Resume at the last committed source record; unreceipted files remain invisible orphans."""
    source_path = resolve_source(settings.evidence_root, source.storage_relative_path)
    last_record = _last_checkpoint(session, job.id)
    job.state = "running"
    job.stage = "ingesting"
    source.parser_revision = PARSER_REVISION
    # Counted once, before parsing, purely so progress has a real denominator.
    # None for formats that cannot be counted without a full parse.
    if job.total_records is None:
        job.total_records = count_records(source_path, source.source_format)
    session.commit()
    parsed_any = False
    batch_number = last_record // settings.ingestion_batch_records
    row_iterator = rows_for_source(source_path, source.source_format)
    seen_txids: dict[str, str | None] = {
        txid: None for txid in _committed_txids(session, evidence_root=settings.evidence_root, job_id=job.id)
    }
    for batch in _chunks(
        (row for row in row_iterator if row.logical_record > last_record), settings.ingestion_batch_records
    ):
        parsed_any = True
        facts: dict[str, list[dict]] = {
            "transactions": [],
            "inputs": [],
            "outputs": [],
            "network_observations": [],
            "quarantine": [],
        }
        accepted = 0
        quarantined = 0
        for row in batch:
            try:
                normalized = normalize_row(
                    case_id=job.case_id, source_id=source.id, source_sha256=source.sha256, row=row
                )
                txid = normalized.facts["transactions"][0]["txid"]
                variant_hash = hashlib.sha256(
                    json.dumps(row.value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
                ).hexdigest()
                if txid in seen_txids:
                    reason = (
                        "conflicting transaction variant for canonical txid"
                        if seen_txids[txid] not in (None, variant_hash)
                        else "duplicate transaction row for canonical txid"
                    )
                    facts["quarantine"].append(
                        _quarantine(
                            case_id=job.case_id,
                            source_id=source.id,
                            source_sha256=source.sha256,
                            row=row,
                            reason=reason,
                        )
                    )
                    quarantined += 1
                    continue
                for record_type, records in normalized.facts.items():
                    facts[record_type].extend(records)
                seen_txids[txid] = variant_hash
                accepted += 1
            except NormalizationError as exc:
                facts["quarantine"].append(
                    _quarantine(
                        case_id=job.case_id, source_id=source.id, source_sha256=source.sha256, row=row, reason=str(exc)
                    )
                )
                quarantined += 1
        _commit_batch(
            session,
            settings=settings,
            job=job,
            source=source,
            batch_number=batch_number,
            rows=batch,
            facts=facts,
            accepted=accepted,
            quarantined=quarantined,
        )
        batch_number += 1
    if not parsed_any and last_record == 0:
        raise SourceParseError("source contains no records")
    # Parsing is only about half of an import; graph build, deterministic
    # findings and the ML layer follow. Those used to run entirely under the
    # "ingesting" stage with no further writes, so a UI polling this job saw
    # nothing change for the whole back half and looked hung. Each stage is now
    # committed as it starts, which is what the intake page reports live.
    def _stage(name: str) -> None:
        job.stage = name
        job.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
        session.commit()

    snapshot = _snapshot_for_job(session, job, source)
    snapshot.state = "graph_building"
    _stage("graph_building")
    graph = build_graph_snapshot(session, evidence_root=settings.evidence_root, snapshot=snapshot)
    graph_record = session.get(GraphSnapshot, graph.graph_snapshot_id)
    if graph_record is None:
        raise RuntimeError("graph snapshot receipt was not persisted")
    _stage("findings")
    finding_count = materialize_findings(
        session, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph_record
    )
    _stage("ml_scoring")
    ml_result = _materialize_ml_findings(session, settings=settings, snapshot=snapshot, graph=graph_record)
    snapshot.provisional = False
    snapshot.state = "complete"
    snapshot.completed_at = utcnow()
    job.state = "completed"
    job.stage = "ingested"
    job.bytes_read = source.byte_size
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = utcnow()
    append_event(
        session,
        case_id=job.case_id,
        event_type="import.completed",
        stage="ingested",
        job=job,
        payload={
            "rows_seen": job.rows_seen,
            "rows_valid": job.rows_accepted,
            "rows_quarantined": job.rows_quarantined,
            "snapshot_id": snapshot.id,
            "graph_snapshot_id": graph.graph_snapshot_id,
            "finding_count": finding_count,
            "ml_finding_count": ml_result["written"],
            "ml_status": ml_result["status"],
            "ml_model_run_id": ml_result.get("model_run_id"),
            "ml_release_id": ml_result.get("release_id"),
            "provisional": False,
        },
    )
    session.commit()
