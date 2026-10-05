"""Receipt-preserving, durable group-only recovery for completed old imports."""
from __future__ import annotations

import hashlib
import threading
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import sessionmaker

from app.engine.graph.query import _path
from app.events import append_event
from app.jobs.analysis import StageTracker
from app.jobs.service import utcnow
from app.models import (
    AnalysisRequest,
    AnalysisStage,
    FindingRecord,
    GraphSnapshot,
    ImportJob,
    InvestigationGroup,
    InvestigationMember,
    InvestigationRun,
    Snapshot,
)


class GroupingProgress:
    """Keep the PostgreSQL lease/progress live without publishing partial groups.

    A separate connection only updates operational rows. The grouping transaction
    never commits until the complete generation is ready. SQLite's single writer
    cannot support this side channel; its progress stays explicitly indeterminate.
    """

    def __init__(self, session, job, lease_seconds):
        self.sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
        self.enabled = session.get_bind().dialect.name == "postgresql"
        self.identity = (job.id, job.attempt, job.lease_owner)
        self.lease_seconds = lease_seconds
        self.interval = max(.1, min(2., lease_seconds / 3))
        self.details = {"phase": "verifying_inputs", "prepared_findings": 0,
                        "total_findings": None, "prepared_groups": 0,
                        "scope": "Prepared work only; groups publish atomically on success"}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.thread = None

    def _persist(self):
        from app.jobs.service import record_worker_heartbeat

        job_id, attempt, owner = self.identity
        with self.lock:
            details = {"grouping_progress": dict(self.details)}
        with self.sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("SET LOCAL statement_timeout = '8s'"))
            # Do not resurrect an expired/reclaimed/completed job owned by another attempt.
            changed = session.execute(update(ImportJob).where(
                ImportJob.id == job_id, ImportJob.attempt == attempt,
                ImportJob.lease_owner == owner, ImportJob.state.in_({"running", "checkpointed"})).values(
                    lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds)))
            if changed.rowcount != 1:
                raise RuntimeError("review grouping lost its worker lease; no generation will be published")
            session.execute(update(AnalysisStage).where(
                AnalysisStage.job_id == job_id, AnalysisStage.attempt == attempt,
                AnalysisStage.name == "investigation_grouping", AnalysisStage.status == "running").values(details=details))
            record_worker_heartbeat(session, owner)

    def _loop(self):
        while not self.stop.wait(self.interval):
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001 - propagate all side-channel failures to the owning worker
                self.error = exc
                return

    def __enter__(self):
        if self.enabled:
            self._persist()
            self.thread = threading.Thread(target=self._loop, name="review-group-progress", daemon=True)
            self.thread.start()
        return self

    def __call__(self, details):
        if self.error is not None:
            raise self.error
        with self.lock:
            self.details.update(details)

    def __exit__(self, exc_type, _exc, _traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive() and exc_type is None:
                raise RuntimeError("review-group progress connection did not stop; retry safely")
        if self.error is not None and exc_type is None:
            raise self.error


def materialize_review_groups(session, *, job, graph, evidence_root, lease_seconds):
    from app.engine.grouping_materializer import materialize

    with GroupingProgress(session, job, lease_seconds) as progress:
        return materialize(session, case_id=job.case_id, snapshot_id=job.snapshot_id,
                           graph=graph, evidence_root=evidence_root, progress=progress)


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


def run_grouping_only(session, *, job, evidence_root, lease_seconds=30):
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
    result = materialize_review_groups(session, job=job, graph=graph,
                                       evidence_root=evidence_root, lease_seconds=lease_seconds)
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
