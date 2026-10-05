"""Batch ingestion with deterministic normalization, immutable fragments, and durable receipts."""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterator
from datetime import timedelta
from itertools import islice

import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.engine.adapters import ParsedRow, SourceParseError, count_records, rows_for_source
from app.engine.canonical.batch import RowOutcome, normalize_one
from app.engine.catalogue import publish_batch
from app.engine.findings import materialize_findings
from app.engine.graph.builder import FactRecords, build_graph_snapshot, load_facts
from app.engine.process_pool import ProcessPool
from app.events import append_event
from app.jobs.service import utcnow
from app.models import EvidenceSource, FragmentReceipt, GraphSnapshot, ImportCheckpoint, ImportJob, Snapshot
from app.resources import current_plan
from app.storage.raw import resolve_source

logger = logging.getLogger(__name__)

PARSER_REVISION = "phase2-ingestion-v1"


def _chunks(rows: Iterator[ParsedRow], size: int) -> Iterator[list[ParsedRow]]:
    while batch := list(islice(rows, size)):
        yield batch


def _normalized_batches(
    batches: Iterator[list[ParsedRow]], *, workers: int, case_id: str, source_id: str, source_sha256: str,
) -> Iterator[tuple[list[ParsedRow], list[RowOutcome]]]:
    """(batch, per-row normalization outcome) in source order; the outcomes
    are computed by `workers` processes when there is more than one."""
    ids = {"case_id": case_id, "source_id": source_id, "source_sha256": source_sha256}
    if workers <= 1:
        for batch in batches:
            yield batch, [normalize_one(row, **ids) for row in batch]
        return
    pending: deque[list[ParsedRow]] = deque()

    def jobs() -> Iterator[dict]:
        for batch in batches:
            pending.append(batch)
            yield {"rows": batch, **ids}

    with ProcessPool(workers) as pool:
        # One batch ahead per worker plus one: enough to keep them busy while
        # this process commits, without holding the source in memory.
        for outcomes in pool.imap("app.engine.canonical.batch:normalize_batch", jobs(), window=workers + 1):
            yield pending.popleft(), outcomes


#: A review budget outside this fraction of the snapshot is not a safe
#: configuration: at or below 0 there is no queue at all, and above half the
#: snapshot "triage" has stopped meaning anything. This is a runtime safety rail
#: on `TRACEX_ML_REVIEW_BUDGET`, independent of whatever numeric value `float()`
#: is willing to parse.
ML_BUDGET_SAFE_RANGE = (0.0, 0.5)


def _materialize_ml_findings(session, *, settings, snapshot, graph, records=None, store=None) -> dict[str, object]:
    """Run the anomaly stack on the completed snapshot, if it is available.

    Deliberately non-fatal. The stack lives behind the optional `ml` extra, and an
    ingestion that has already committed correct evidence and correct
    deterministic findings must not be failed because a ranking could not be
    produced. Every outcome is recorded on the completion event so a silent skip
    is still visible.

    Default v2 is unsupervised. Owner-trusted frozen candidates require explicit
    case-mode eligibility; imports never open truth or learn criminality reviews.
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
        from app.ml.candidate_findings import score_or_fallback
    except ImportError:
        # scikit-learn/numpy absent: Phase 4 findings still stand on their own.
        return {"written": 0, "status": "unavailable"}
    try:
        result, eligibility_decision = score_or_fallback(
            session, settings=settings, snapshot=snapshot, graph=graph, budget=float(budget),
            records=records, store=store,
        )
    except ImportError:
        return {"written": 0, "status": "unavailable", "reason": "required ML runtime dependency is unavailable"}
    except Exception as error:  # noqa: BLE001 - a ranking failure must not lose the import
        session.rollback()
        logger.warning("anomaly stack did not run for snapshot %s: %s", snapshot.id, error)
        return {"written": 0, "status": "failed", "legacy_status": f"error:{type(error).__name__}",
                "reason": f"{type(error).__name__}: {str(error)[:1000]}"}
    return {
        "written": result.written,
        "status": "insufficient_data" if result.scored_transactions < 50
                  else "written" if result.written else "no_rows_flagged",
        "scored_transactions": result.scored_transactions,
        "threshold_flagged": result.flagged,
        "model_run_id": result.model_run_id,
        "release_id": result.release_id,
        "eligibility_decision": eligibility_decision,
        "global_memory_estimate_bytes": getattr(store, "ml_memory_estimate_bytes", None),
        "joint_memory_estimate_bytes": getattr(store, "ml_joint_memory_estimate_bytes", None),
    }


def _materialize_analytics(session, *, settings, snapshot, graph, records=None, store=None, refresh=False) -> dict[str, object]:
    """Entity clusters, Geo-IP enrichment, network correlation, embeddings.

    Non-fatal for the same reason as the anomaly stack: the evidence, graph and
    findings are already correct and committed; a failure here is recorded on
    the completion event instead of losing the import.
    """
    try:
        from app.engine.analytics import build_analytics

        result = build_analytics(
            session, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph, records=records,
            store=store, refresh=refresh,
        )
    except Exception as error:
        session.rollback()
        logger.warning("analytics did not run for snapshot %s: %s", snapshot.id, error, exc_info=True)
        return {"status": "failed", "reason": f"{type(error).__name__}: {str(error)[:1000]}", "network_findings": 0}
    return {
        "status": "complete",
        "analytics_snapshot_id": result.analytics_snapshot_id,
        "network_findings": result.network_finding_count,
        "entity_count": result.summary.get("entity_count"),
        "geoip_installed": result.summary.get("geoip_installed", False),
        "summary": result.summary,
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
    canonical: dict[str, list[str]] | None = None,
) -> None:
    artifacts = publish_batch(
        evidence_root=settings.evidence_root,
        case_id=job.case_id,
        source_sha256=source.sha256,
        source_id=source.id,
        logical_batch=batch_number,
        facts=facts,
        canonical=canonical,
    )
    snapshot = _snapshot_for_job(session, job, source)
    from app.models import AddressActivity

    activity = {}
    for kind in ("inputs", "outputs"):
        for fact in facts[kind]:
            address = fact.get("address")
            if address:
                txs, count = activity.get(address, (set(), 0))
                txs.add(fact["txid"])
                activity[address] = txs, count + 1
    if activity:
        dialect = session.bind.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as upsert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as upsert
        else:
            raise RuntimeError("provisional activity requires SQLite or PostgreSQL")
        statement = upsert(AddressActivity)
        statement = statement.on_conflict_do_update(index_elements=["job_id", "address"], set_={
            "transactions": AddressActivity.transactions + statement.excluded.transactions,
            "participations": AddressActivity.participations + statement.excluded.participations,
            "last_batch": statement.excluded.last_batch})
        session.execute(statement, [{"job_id": job.id, "case_id": job.case_id, "address": address,
            "transactions": len(txs), "participations": count, "last_batch": batch_number}
            for address, (txs, count) in sorted(activity.items())])
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
            "provisional_addresses_in_batch": len(activity),
        },
    )
    session.commit()


def ingest_source(session: Session, *, settings: Settings, job: ImportJob, source: EvidenceSource) -> None:
    """Resume at the last committed source record; unreceipted files remain invisible orphans."""
    if job.state == "completed":
        return
    source_path = resolve_source(settings.evidence_root, source.storage_relative_path)
    last_record = _last_checkpoint(session, job.id)
    from app.jobs.analysis import SUCCESS, StageTracker, analysis_view, stage_rows

    prior_stages = {row.name: row for row in stage_rows(session, job.id)}
    from app.engine import geoip
    from app.engine.analytics import latest_analytics
    from app.models import AnalysisRequest

    refresh_requests = list(session.scalars(select(AnalysisRequest).where(
        AnalysisRequest.job_id == job.id, AnalysisRequest.attempt <= job.attempt,
        AnalysisRequest.refresh_analytics.is_(True), AnalysisRequest.fulfilled.is_(False))))
    prior_analytics = latest_analytics(session, job.case_id, job.snapshot_id) if job.snapshot_id else None
    refresh_analytics = bool(refresh_requests) or bool(
        prior_analytics and not prior_analytics.summary.get("geoip_installed") and geoip.status().get("installed"))
    ingestion_done = prior_stages.get("ingesting") is not None and prior_stages["ingesting"].status in SUCCESS
    tracker = StageTracker(session, job)
    tracker.start("ingesting", reuse_completed=ingestion_done)
    job.state = "running"
    job.stage = "ingesting"
    source.parser_revision = PARSER_REVISION
    # Counted once, before parsing, purely so progress has a real denominator.
    # None for formats that cannot be counted without a full parse.
    if job.total_records is None:
        job.total_records = count_records(source_path, source.source_format)
    session.commit()
    parsed_any = False
    # Batch size adapts to this machine's free memory (never above the
    # configured size, so a well-provisioned host keeps the shipped default).
    plan = current_plan()
    batch_records = plan.ingestion_batch_records(settings.ingestion_batch_records)
    parse_workers = plan.worker_processes(records=job.total_records)
    # Several normalized batches and their source rows coexist in the ordered
    # process window. Divide the batch allowance across those live copies.
    if parse_workers > 1:
        batch_records = max(2048, batch_records // (parse_workers + 1))
    # Continue after the highest committed batch rather than dividing by the
    # batch size: a resumed attempt may run with a different size.
    last_batch = session.scalar(select(func.max(FragmentReceipt.logical_batch)).where(FragmentReceipt.job_id == job.id))
    batch_number = 0 if last_record == 0 or last_batch is None else last_batch + 1
    row_iterator = iter(()) if ingestion_done else rows_for_source(source_path, source.source_format)
    # Binary txid -> 16-byte digest of the first source variant: about half the
    # memory of hex-string keys and values, which matters at millions of rows.
    seen_txids: dict[bytes, bytes | None] = {
        bytes.fromhex(txid): None
        for txid in (() if ingestion_done else _committed_txids(session, evidence_root=settings.evidence_root, job_id=job.id))
    }
    # On a fresh (non-resumed) run every receipted fact is produced right here,
    # so the later stages can take them from memory instead of re-reading and
    # re-decoding every fragment. Checkpoint and receipts commit together, so
    # no checkpoint means no receipted fragment from an earlier attempt. A
    # resumed job falls back to reading the committed fragments.
    # The machine decides how the post-parse stages run: fully in memory
    # (fastest) when the whole snapshot fits its budget, otherwise bounded --
    # staged on disk and processed a chunk/partition at a time (app.engine.bounded).
    execution_mode = plan.execution_mode(records=job.total_records, source_bytes=source.byte_size)
    collected: FactRecords | None = (
        {"transactions": [], "inputs": [], "outputs": [], "network_observations": []}
        if last_record == 0 and execution_mode == "memory"
        else None
    )
    # Source column -> canonical field renames applied by the schema profile
    # (app.engine.canonical.profiles); reported on the completion event.
    field_mapping: dict[str, str] = {}
    # Normalizing rows is the CPU-heavy half of parsing; on a large import it
    # runs in worker processes while this one deduplicates and commits, in
    # source order, exactly as the in-process loop does.
    batches = _normalized_batches(
        _chunks((row for row in row_iterator if row.logical_record > last_record), batch_records),
        workers=parse_workers,
        case_id=job.case_id, source_id=source.id, source_sha256=source.sha256,
    )
    for batch, outcomes in batches:
        parsed_any = True
        facts: dict[str, list[dict]] = {
            "transactions": [],
            "inputs": [],
            "outputs": [],
            "network_observations": [],
            "quarantine": [],
        }
        canonical: dict[str, list[str]] = {record_type: [] for record_type in facts}
        accepted = 0
        quarantined = 0
        for row, outcome in zip(batch, outcomes, strict=True):
            if outcome.error is not None:
                facts["quarantine"].append(
                    _quarantine(
                        case_id=job.case_id, source_id=source.id, source_sha256=source.sha256, row=row,
                        reason=outcome.error,
                    )
                )
                quarantined += 1
                continue
            if outcome.field_mapping:
                field_mapping.update(outcome.field_mapping)
            txid, variant_hash = outcome.txid, outcome.variant_hash
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
            for record_type, records in outcome.facts.items():
                facts[record_type].extend(records)
                canonical[record_type].extend(outcome.canonical[record_type])
            seen_txids[txid] = variant_hash
            accepted += 1
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
            canonical=canonical,
        )
        if collected is not None:
            # Same order load_facts reads them back in: per record type, by batch.
            for record_type, records in collected.items():
                records.extend(facts[record_type])
        batch_number += 1
    if not parsed_any and last_record == 0:
        raise SourceParseError("source contains no records")
    # Parsing is only about half of an import; graph build, deterministic
    # findings and the ML layer follow. Those used to run entirely under the
    # "ingesting" stage with no further writes, so a UI polling this job saw
    # nothing change for the whole back half and looked hung. Each stage is now
    # committed as it starts, which is what the intake page reports live.
    def _stage(name: str) -> None:
        from app.engine.confidence import pin_snapshot_confidence

        if tracker.row is not None and tracker.row.name == "findings":
            pin_snapshot_confidence(session, snapshot.id)
        job.stage = name
        job.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
        session.commit()
        tracker.start(name, reuse_completed=name != "analytics" or not refresh_analytics)

    def _ml_done(result):
        from app.engine.confidence import pin_snapshot_confidence

        pin_snapshot_confidence(session, snapshot.id)
        tracker.finish(status=result["status"], reason=result.get("reason", result["status"] if result["status"]
                       not in {"written", "no_rows_flagged"} else None), details=result)

    tracker.finish(details={"rows_seen": job.rows_seen, "rows_accepted": job.rows_accepted,
                            "rows_quarantined": job.rows_quarantined})

    snapshot = _snapshot_for_job(session, job, source)
    snapshot.state = "graph_building"
    _stage("graph_building")
    # Re-check with the exact accepted count now that parsing is done.
    execution_mode = plan.execution_mode(records=job.rows_accepted or job.total_records, source_bytes=source.byte_size)
    if execution_mode == "memory":
        # Loaded once and shared: graph build, deterministic findings and the
        # anomaly stack each used to re-read and re-parse every committed fragment.
        records = collected if collected is not None else load_facts(session, settings.evidence_root, snapshot.id)
        collected = None
        graph = build_graph_snapshot(session, evidence_root=settings.evidence_root, snapshot=snapshot, records=records)
        graph_record = session.get(GraphSnapshot, graph.graph_snapshot_id)
        if graph_record is None:
            raise RuntimeError("graph snapshot receipt was not persisted")
        _stage("findings")
        finding_count = materialize_findings(
            session, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph_record, records=records
        )
        _stage("ml_scoring")
        ml_result = _materialize_ml_findings(
            session, settings=settings, snapshot=snapshot, graph=graph_record, records=records,
        )
        session.commit()
        _ml_done(ml_result)
        _stage("analytics")
        analytics_result = _materialize_analytics(
            session, settings=settings, snapshot=snapshot, graph=graph_record, records=records,
            refresh=refresh_analytics,
        )
        del records
    else:
        collected = None
        from app.engine.bounded import FactStore, build_graph_bounded, materialize_findings_bounded

        with FactStore.build(session, evidence_root=settings.evidence_root, snapshot=snapshot) as store:
            graph = build_graph_bounded(session, store=store, evidence_root=settings.evidence_root, snapshot=snapshot)
            graph_record = session.get(GraphSnapshot, graph.graph_snapshot_id)
            if graph_record is None:
                raise RuntimeError("graph snapshot receipt was not persisted")
            _stage("findings")
            finding_count = materialize_findings_bounded(
                session, store=store, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph_record
            )
            _stage("ml_scoring")
            ml_result = _materialize_ml_findings(
                session, settings=settings, snapshot=snapshot, graph=graph_record, store=store,
            )
            session.commit()
            _ml_done(ml_result)
            _stage("analytics")
            analytics_result = _materialize_analytics(
                session, settings=settings, snapshot=snapshot, graph=graph_record, store=store,
                refresh=refresh_analytics,
            )
    if analytics_result["status"] == "complete":
        for request in refresh_requests:
            request.fulfilled = True
    # Publish the revision, completion of its durable request and stage outcome
    # in one transaction. A killed/failed attempt leaves the intent pending.
    tracker.finish(status=analytics_result["status"], reason=analytics_result.get("reason"), details=analytics_result)
    if analytics_result["status"] == "complete":
        tracker.start("geoip", reuse_completed=not refresh_analytics)
        tracker.finish(status="complete" if analytics_result.get("geoip_installed") else "unavailable",
                       reason=None if analytics_result.get("geoip_installed") else "offline Geo-IP database missing",
                       details={"installed": bool(analytics_result.get("geoip_installed"))})
        summary = analytics_result.get("summary", {})
        tracker.start("network_coverage", reuse_completed=not refresh_analytics)
        observed = int(summary.get("observed_transactions", 0))
        tracker.finish(status="complete" if observed >= job.rows_accepted else "incomplete",
                       reason=None if observed >= job.rows_accepted else "network metadata unavailable for some transactions",
                       details={"observed_transactions": observed, "canonical_transactions": job.rows_accepted})
        tracker.start("embeddings", reuse_completed=not refresh_analytics)
        embedding_status = summary.get("embedding_status", "missing")
        tracker.finish(status="complete" if embedding_status == "complete" else
                       "no_rows_flagged" if embedding_status == "no wallet active in two transactions" else "unavailable",
                       reason=None if embedding_status == "complete" else embedding_status,
                       details={"embedded_wallets": summary.get("embedded_wallets", 0)})
    from app.engine.confidence import pin_snapshot_confidence
    from app.engine.investigations import materialize
    pin_snapshot_confidence(session, snapshot.id)
    _stage("investigation_grouping")
    grouping = materialize(session, case_id=job.case_id, snapshot_id=snapshot.id,
                           graph=session.get(GraphSnapshot, graph.graph_snapshot_id), evidence_root=settings.evidence_root)
    from app.jobs.grouping import fulfill_grouping_requests

    fulfill_grouping_requests(session, job)
    tracker.finish(details=grouping)
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
            "ml_status": ml_result.get("legacy_status", ml_result["status"]),
            "ml_model_run_id": ml_result.get("model_run_id"),
            "ml_release_id": ml_result.get("release_id"),
            "execution_mode": execution_mode,
            "field_mapping": field_mapping,
            "analytics_status": analytics_result["status"],
            "analytics_snapshot_id": analytics_result.get("analytics_snapshot_id"),
            "network_finding_count": analytics_result.get("network_findings", 0),
            "entity_count": analytics_result.get("entity_count"),
            "provisional": False,
            "analysis": analysis_view(session, job),
        },
    )
    session.commit()
