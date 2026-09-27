"""Batch ingestion with deterministic normalization, immutable fragments, and durable receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import timedelta
from itertools import islice

import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.engine.adapters import ParsedRow, SourceParseError, rows_for_source
from app.engine.canonical import NormalizationError, normalize_row
from app.engine.catalogue import publish_batch
from app.engine.findings import materialize_findings
from app.engine.graph.builder import build_graph_snapshot
from app.events import append_event
from app.jobs.service import utcnow
from app.models import EvidenceSource, FragmentReceipt, GraphSnapshot, ImportCheckpoint, ImportJob, Snapshot
from app.storage.raw import resolve_source

PARSER_REVISION = "phase2-ingestion-v1"


def _chunks(rows: Iterator[ParsedRow], size: int) -> Iterator[list[ParsedRow]]:
    while batch := list(islice(rows, size)):
        yield batch


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
    snapshot = _snapshot_for_job(session, job, source)
    snapshot.state = "graph_building"
    graph = build_graph_snapshot(session, evidence_root=settings.evidence_root, snapshot=snapshot)
    graph_record = session.get(GraphSnapshot, graph.graph_snapshot_id)
    if graph_record is None:
        raise RuntimeError("graph snapshot receipt was not persisted")
    finding_count = materialize_findings(
        session, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph_record
    )
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
            "provisional": False,
        },
    )
    session.commit()
