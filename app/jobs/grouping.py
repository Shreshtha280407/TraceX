"""Receipt-preserving, durable group-only recovery for completed old imports."""
from __future__ import annotations

import hashlib

from sqlalchemy import select, update

from app.engine.graph.query import _path
from app.events import append_event
from app.jobs.analysis import StageTracker
from app.jobs.service import utcnow
from app.models import (
    AnalysisRequest,
    FindingRecord,
    GraphSnapshot,
    InvestigationGroup,
    InvestigationMember,
    InvestigationRun,
    Snapshot,
)


def published_membership(case_id):
    return select(InvestigationMember.finding_id).join(InvestigationGroup).join(InvestigationRun).where(
        InvestigationMember.finding_id == FindingRecord.id, InvestigationRun.case_id == case_id,
        InvestigationRun.active.is_(True), InvestigationRun.state == "complete").exists()


def latest_pending_request(session, job):
    return session.scalar(select(AnalysisRequest).where(
        AnalysisRequest.job_id == job.id, AnalysisRequest.attempt <= job.attempt,
        AnalysisRequest.fulfilled.is_(False)).order_by(AnalysisRequest.attempt.desc()).limit(1))


def fulfill_grouping_requests(session, job):
    session.execute(update(AnalysisRequest).where(
        AnalysisRequest.job_id == job.id, AnalysisRequest.attempt <= job.attempt,
        AnalysisRequest.grouping_only.is_(True), AnalysisRequest.fulfilled.is_(False)).values(fulfilled=True))


def run_grouping_only(session, *, job, evidence_root):
    """Return False for ordinary/full retries; never rerun their stages here.

    A newer full-analysis request supersedes group-only intent. Conversely, an
    automatic retry or lease recovery resumes the same unfulfilled request.
    """
    request = latest_pending_request(session, job)
    if request is None or not request.grouping_only:
        return False
    snapshot = session.get(Snapshot, job.snapshot_id)
    graph = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == job.snapshot_id,
                                                       GraphSnapshot.case_id == job.case_id))
    if (snapshot is None or snapshot.case_id != job.case_id or snapshot.job_id != job.id
            or snapshot.state != "complete" or snapshot.provisional or graph is None or graph.state != "complete"):
        raise RuntimeError("group-only recovery requires the original completed snapshot and canonical graph")
    job.stage = "investigation_grouping"
    tracker = StageTracker(session, job)
    tracker.start("investigation_grouping")
    # No graph reconstruction or global ML arrays. Verify the persisted graph
    # before using it as the immutable connectivity basis for grouping.
    with _path(evidence_root, graph.storage_relative_path).open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != graph.sha256:
            raise RuntimeError("immutable canonical graph hash mismatch")
    from app.engine.investigations import materialize

    result = materialize(session, case_id=job.case_id, snapshot_id=job.snapshot_id,
                         graph=graph, evidence_root=evidence_root)
    # An identical complete generation may exist but be inactive. Reactivate
    # only the digest-matched generation returned by the frozen procedure;
    # membership and historical reviewer decisions are not edited or copied.
    session.execute(update(InvestigationRun).where(
        InvestigationRun.snapshot_id == job.snapshot_id, InvestigationRun.id != result["run_id"]).values(active=False))
    session.get(InvestigationRun, result["run_id"]).active = True
    fulfill_grouping_requests(session, job)
    job.state, job.stage = "completed", "ingested"
    job.lease_owner = job.lease_expires_at = job.error_code = job.error_detail = None
    job.completed_at = utcnow()
    # Stage finish, generation publication, fulfillment and job completion
    # share one commit: a crash cannot lose group-only intent in between.
    append_event(session, case_id=job.case_id, event_type="import.completed", stage=job.stage, job=job,
                 payload={"grouping_only": True, "snapshot_id": job.snapshot_id,
                          "rows_valid": job.rows_accepted, "grouping": result})
    tracker.finish(details={**result, "grouping_only": True})
    # analysis_view still reflects missing/degraded prior stages; group-only
    # completion is never a fresh all-stage benchmark or a quality claim.
    return True
