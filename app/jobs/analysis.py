"""Durable analysis outcomes, distinct from successful evidence ingestion."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import select

from app.events import append_event
from app.models import AnalysisStage

REQUIRED_STAGES = ("source_verification", "ingesting", "graph_building", "findings", "ml_scoring", "analytics")
SUCCESS = {"complete", "written", "no_rows_flagged"}


def _child_cpu_seconds():
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime
    except ImportError:
        return None


def stage_rows(session, job_id: str) -> list[AnalysisStage]:
    return list(session.scalars(select(AnalysisStage).where(AnalysisStage.job_id == job_id)
                               .order_by(AnalysisStage.attempt, AnalysisStage.started_at, AnalysisStage.name)))


def analysis_view(session, job) -> dict:
    latest = {row.name: row for row in stage_rows(session, job.id)}
    stages = [{"name": name, "status": row.status, "reason": row.reason,
               "attempt": row.attempt, "started_at": row.started_at.isoformat(),
               "completed_at": row.completed_at.isoformat() if row.completed_at else None,
               "duration_seconds": row.duration_seconds, "details": row.details}
              for name, row in latest.items()]
    if job.state == "failed":
        state = "failed"
    elif job.state != "completed":
        state = "running"
    elif all(name in latest and latest[name].status in SUCCESS for name in REQUIRED_STAGES):
        state = "degraded" if any(row.status not in SUCCESS for row in latest.values()) else "complete"
    else:
        state = "degraded" if latest else "unknown"
    return {"state": state, "stages": stages, "required_stages": list(REQUIRED_STAGES),
            "retry_supported": job.state in {"failed", "completed"} and state != "complete"}


class StageTracker:
    def __init__(self, session, job):
        self.session, self.job = session, job
        self.row = None
        self.clock = None
        self.cpu_clock = None
        self.child_cpu_clock = None

    def start(self, name: str, *, reuse_completed: bool = False):
        self.finish()
        if reuse_completed:
            prior = next((row for row in reversed(stage_rows(self.session, self.job.id)) if row.name == name), None)
            if prior is not None and prior.status in SUCCESS:
                # Keep the original successful outcome/time/evidence pinned.
                # The caller must only reuse this for idempotent materializers.
                return
        self.row = AnalysisStage(job_id=self.job.id, case_id=self.job.case_id, name=name,
                                 attempt=self.job.attempt, status="running", started_at=datetime.now(UTC), details={})
        self.session.add(self.row)
        self.session.flush()
        self.clock = time.monotonic()
        self.cpu_clock = time.process_time()
        self.child_cpu_clock = _child_cpu_seconds()
        append_event(self.session, case_id=self.job.case_id, event_type="analysis.stage_started",
                     stage=name, job=self.job, payload={"analysis_stage": name, "status": "running"})
        self.session.commit()

    def finish(self, status="complete", reason=None, details=None):
        if self.row is None:
            return
        self.row.status, self.row.reason = status, reason
        self.row.completed_at = datetime.now(UTC)
        self.row.duration_seconds = time.monotonic() - self.clock
        child_cpu = _child_cpu_seconds()
        self.row.details = {**(details or {}), "worker_cpu_seconds": time.process_time() - self.cpu_clock,
                            "reaped_child_cpu_seconds": child_cpu - self.child_cpu_clock
                            if child_cpu is not None and self.child_cpu_clock is not None else None,
                            "child_cpu_scope": "reaped direct children; unavailable on unsupported platforms",
                            "cpu_scope": "parent worker only; excludes child CPU"}
        append_event(self.session, case_id=self.job.case_id, event_type="analysis.stage_finished",
                     stage=self.row.name, job=self.job,
                     payload={"analysis_stage": self.row.name, "status": status, "reason": reason,
                              "duration_seconds": self.row.duration_seconds, "details": self.row.details})
        self.session.commit()
        self.row = None


def fail_running_stage(session, job, reason):
    for row in stage_rows(session, job.id):
        if row.status == "running":
            row.status, row.reason = "failed", reason[:2000]
            now = datetime.now(UTC)
            row.completed_at = now
            start = row.started_at.replace(tzinfo=UTC) if row.started_at.tzinfo is None else row.started_at
            row.duration_seconds = max(0.0, (now - start).total_seconds())
