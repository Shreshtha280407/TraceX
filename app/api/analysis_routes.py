"""Case-authorized analysis status, review capacity and receipt-only activity."""
# ruff: noqa: B008
from __future__ import annotations

import math
from datetime import UTC, datetime

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_case_member
from app.db import get_session
from app.engine.graph.builder import _receipt_path
from app.events import append_event
from app.jobs.analysis import analysis_view
from app.jobs.service import job_view
from app.ml_release_constants import ML_RULE_VERSION
from app.models import (
    AddressActivity,
    AnalysisRequest,
    AuditRecord,
    FindingRecord,
    FragmentReceipt,
    GraphSnapshot,
    ImportJob,
    User,
)

router = APIRouter(prefix="/v1")


@router.get("/cases/{case_id}/analyst-label-contract")
def label_contract(case_id: str, snapshot_id: str, cutoff: datetime | None = None,
                   user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_case_member(case_id, user, session)
    if not session.scalar(select(ImportJob.id).where(ImportJob.case_id == case_id,
                                                   ImportJob.snapshot_id == snapshot_id)):
        raise HTTPException(404, "Snapshot not found")
    if cutoff is not None and cutoff.tzinfo is None:
        raise HTTPException(422, "cutoff must include a timezone")
    from app.ml.analyst_labels import analyst_label_contract

    return analyst_label_contract(session, case_id=case_id, snapshot_id=snapshot_id, cutoff=cutoff or datetime.now(UTC))


def evidence_root():
    from app.api.routes import settings

    return settings.evidence_root


def scoped_job(session, case_id, job_id=None):
    query = select(ImportJob).where(ImportJob.case_id == case_id)
    if job_id:
        query = query.where(ImportJob.id == job_id)
    job = session.scalar(query.order_by(ImportJob.created_at.desc(), ImportJob.id).limit(1))
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/cases/{case_id}/analysis")
def status(case_id: str, job_id: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_case_member(case_id, user, session)
    job = scoped_job(session, case_id, job_id)
    counts = dict(session.execute(select(FragmentReceipt.record_type, func.sum(FragmentReceipt.record_count))
                                 .where(FragmentReceipt.job_id == job.id).group_by(FragmentReceipt.record_type)).all())
    graph = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == job.snapshot_id))
    from app.engine.analytics import latest_analytics

    analytics = latest_analytics(session, case_id, job.snapshot_id) if job.snapshot_id else None
    return {"case_id": case_id, "job_id": job.id, "snapshot_id": job.snapshot_id, "analysis": analysis_view(session, job),
            "canonical_counts": counts, "graph": {"nodes": graph.node_count, "edges": graph.edge_count,
            "sha256": graph.sha256,
            "coverage": graph.coverage} if graph else None, "analytics": analytics.summary if analytics else None}


@router.post("/cases/{case_id}/analysis/retry", status_code=202)
def retry(case_id: str, job_id: str, recompute_analytics: bool = False,
          user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_case_member(case_id, user, session)
    job = session.scalar(select(ImportJob).where(ImportJob.case_id == case_id, ImportJob.id == job_id).with_for_update())
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.state not in {"failed", "completed"} or (
            not recompute_analytics and not analysis_view(session, job)["retry_supported"]):
        raise HTTPException(409, "Job is active or analysis is already complete")
    session.add(AnalysisRequest(job_id=job.id, attempt=job.attempt + 1, refresh_analytics=recompute_analytics))
    job.state, job.stage = "queued", "queued"
    job.error_code = job.error_detail = job.completed_at = job.lease_owner = job.lease_expires_at = None
    append_event(session, case_id=case_id, event_type="analysis.retry_queued", stage="queued", job=job,
                 payload={"resume_from_receipts": True, "recompute_analytics": recompute_analytics})
    session.add(AuditRecord(case_id=case_id, actor_id=user.id, action="analysis.retry", target_type="import_job",
                            target_id=job.id, detail={"previous_attempt": job.attempt,
                                                   "recompute_analytics": recompute_analytics}))
    session.commit()
    return job_view(job, session)


@router.get("/cases/{case_id}/review-queue")
def review_queue(case_id: str, snapshot_id: str | None = None, k: int | None = Query(None, ge=0),
                 fraction: float | None = Query(None, ge=0, le=1), limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0), review_state: str | None = None,
                 user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_case_member(case_id, user, session)
    if k is not None and fraction is not None:
        raise HTTPException(422, "choose integer k or fraction")
    job = scoped_job(session, case_id)
    if snapshot_id:
        job = session.scalar(select(ImportJob).where(ImportJob.case_id == case_id, ImportJob.snapshot_id == snapshot_id))
        if job is None:
            raise HTTPException(404, "Snapshot not found")
    if job.state != "completed":
        raise HTTPException(409, "Queue requires a finalized snapshot")
    if review_state not in {None, "open", "triaged", "confirmed", "dismissed", "escalated", "needs_data_review"}:
        raise HTTPException(422, "unsupported review state")
    stages = {s["name"]: s for s in analysis_view(session, job)["stages"]}
    scored = int(stages.get("ml_scoring", {}).get("details", {}).get("scored_transactions", 0))
    scorer = stages.get("ml_scoring", {}).get("details", {}).get("release_id", ML_RULE_VERSION)
    capacity = k if k is not None else math.floor(scored * (fraction if fraction is not None else .01))
    scope = (FindingRecord.case_id == case_id, FindingRecord.snapshot_id == job.snapshot_id,
             FindingRecord.rule_version == scorer)
    flagged_count, transaction_count = session.execute(select(func.count(FindingRecord.id),
        func.count(func.distinct(FindingRecord.entity_ref))).where(*scope)).one()
    ranked = select(FindingRecord.entity_ref.label("entity"), func.max(FindingRecord.raw_score).label("score"))\
        .where(*scope).group_by(FindingRecord.entity_ref)\
        .order_by(func.max(FindingRecord.raw_score).desc(), FindingRecord.entity_ref).limit(capacity).subquery()
    page_query = select(ranked.c.entity, ranked.c.score)
    if review_state:
        reviewed = select(FindingRecord.entity_ref).where(*scope, FindingRecord.status == review_state)
        page_query = page_query.where(ranked.c.entity.in_(reviewed))
    filtered_total = session.scalar(select(func.count()).select_from(page_query.subquery()))
    page = session.execute(page_query.order_by(ranked.c.score.desc(), ranked.c.entity).limit(limit).offset(offset)).all()
    entities = [row.entity for row in page]
    grouped = {entity: [] for entity in entities}
    for finding in session.scalars(select(FindingRecord).where(*scope, FindingRecord.entity_ref.in_(entities))
                                  .order_by(FindingRecord.rule_id, FindingRecord.id)):
        grouped[finding.entity_ref].append(finding)
    deterministic = {}
    associations = [FindingRecord.entity_ref.in_(entities)]
    # Fixed-length transaction IDs are matched as complete JSON string tokens.
    path_nodes = cast(FindingRecord.feature_vector["detector_result"]["graph_path"]["nodes"], Text)
    associations.extend(path_nodes.contains('"' + entity + '"') for entity in entities)
    for finding in session.scalars(select(FindingRecord).where(FindingRecord.case_id == case_id,
                                  FindingRecord.snapshot_id == job.snapshot_id,
                                  ~FindingRecord.rule_version.like("anomaly-stack-%"), or_(*associations))):
        refs = [finding.entity_ref]
        detector = (finding.feature_vector or {}).get("detector_result", {})
        refs.extend((detector.get("graph_path") or {}).get("nodes", []))
        for ref in refs:
            if ref.startswith("tx:"):
                deterministic.setdefault(ref, set()).add(finding.id)
    return {"case_id": case_id, "snapshot_id": job.snapshot_id, "policy_version": "ml-top-k-v1",
            "scorer": scorer, "eligibility_decision": stages.get("ml_scoring", {}).get("details", {}).get("eligibility_decision"),
            "eligible_scored_transactions": scored,
            "threshold_flagged_transactions": transaction_count, "threshold_flagged_findings": flagged_count,
            "capacity": capacity, "queued_transactions": min(capacity, transaction_count),
            "additional_flagged_transactions": max(0, transaction_count - capacity), "filtered_total": filtered_total,
            "rounding": "floor; zero is allowed; never fill with unflagged transactions",
            "tie_order": "raw_score descending, entity_ref ascending; one frozen ML scorer only",
            "limit": limit, "offset": offset, "items": [{"transaction": row.entity, "score": row.score,
                "finding_ids": [f.id for f in grouped[row.entity]],
                "deterministic_finding_ids": sorted(deterministic.get(row.entity, [])),
                "statuses": sorted({f.status for f in grouped[row.entity]})} for row in page]}


@router.get("/cases/{case_id}/activity")
def activity(case_id: str, job_id: str | None = None, limit: int = Query(50, ge=1, le=200),
             offset: int = Query(0, ge=0), user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_case_member(case_id, user, session)
    job = scoped_job(session, case_id, job_id)
    total = session.scalar(select(func.count()).select_from(AddressActivity).where(AddressActivity.job_id == job.id))
    if total:
        rows = list(session.scalars(select(AddressActivity).where(AddressActivity.job_id == job.id)
                                   .order_by(AddressActivity.address).limit(limit).offset(offset)))
        return {"case_id": case_id, "job_id": job.id, "snapshot_id": job.snapshot_id,
                "provisional": job.state != "completed", "read_model": "receipt-committed address participation v1",
                "total": total, "limit": limit, "offset": offset,
                "entities": [{"address": row.address, "transactions": row.transactions,
                              "participations": row.participations} for row in rows]}
    receipts = list(session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job.id,
                                  FragmentReceipt.record_type.in_(["inputs", "outputs"]))
                                   .order_by(FragmentReceipt.logical_batch, FragmentReceipt.record_type)))
    paths = [str(_receipt_path(evidence_root(), r.storage_relative_path)) for r in receipts]
    if not paths:
        return {"case_id": case_id, "job_id": job.id, "provisional": True, "total": 0, "entities": []}
    con = duckdb.connect(config={"threads": 1, "memory_limit": "128MB"})
    try:
        con.execute("CREATE TEMP TABLE activity AS SELECT canonical_json ->> '$.address' AS address, "
                    "count(DISTINCT txid) AS transactions, count(*) AS participations FROM read_parquet(?) "
                    "WHERE NULLIF(canonical_json ->> '$.address', '') IS NOT NULL GROUP BY address", [paths])
        total = con.execute("SELECT count(*) FROM activity").fetchone()[0]
        rows = con.execute("SELECT address, transactions, participations FROM activity ORDER BY address LIMIT ? OFFSET ?",
                           [limit, offset]).fetchall()
    finally:
        con.close()
    return {"case_id": case_id, "job_id": job.id, "snapshot_id": job.snapshot_id, "provisional": job.state != "completed",
            "read_model": "receipt-approved address participation; no inferred ownership or spend links",
            "receipt_count": len(receipts), "total": total, "limit": limit, "offset": offset,
            "entities": [{"address": address, "transactions": txs, "participations": count} for address, txs, count in rows]}
