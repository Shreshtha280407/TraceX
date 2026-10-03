"""Idempotent, durable job lifecycle and lease management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.events import append_event
from app.models import AuditRecord, EvidenceSource, ImportJob, User, WorkerHeartbeat

ACTIVE_STATES = {"queued", "running", "checkpointed"}


def utcnow() -> datetime:
    return datetime.now(UTC)


# An import is parse -> graph build -> deterministic findings -> ML scoring ->
# entity/network analytics.
# Parsing is the only stage with a natural row-level denominator, so the others
# are represented as the share of total wall time they actually take on a
# measured run rather than being averaged into a meaningless single bar.
_STAGE_SPAN: dict[str, tuple[float, float]] = {
    "queued": (0.0, 0.0),
    "source_verification": (0.0, 0.0),
    "ingesting": (0.0, 0.55),
    "graph_building": (0.55, 0.70),
    "findings": (0.70, 0.92),
    "ml_scoring": (0.92, 0.97),
    "analytics": (0.97, 0.995),
    "ingested": (1.0, 1.0),
}


def job_progress(job: ImportJob) -> dict:
    """Real progress where it can be measured, and an explicit admission where
    it cannot -- never a fabricated moving number."""
    if job.state == "completed":
        counted = f"{job.rows_seen:,} of {job.total_records:,}" if job.total_records else f"{job.rows_seen:,}"
        return {
            "percent": 100.0,
            "basis": f"completed · {counted} records parsed · {job.rows_accepted:,} accepted · {job.rows_quarantined:,} quarantined",
            "determinate": True,
        }
    if job.state == "failed":
        return {"percent": None, "basis": "failed", "determinate": False}
    start, end = _STAGE_SPAN.get(job.stage, (0.0, 0.55))
    if job.stage in ("ingesting", "queued") and job.total_records:
        fraction = min(1.0, job.rows_seen / job.total_records)
        return {
            "percent": round((start + (end - start) * fraction) * 100, 1),
            "basis": f"{job.rows_seen:,} of {job.total_records:,} records parsed",
            "determinate": True,
        }
    if job.stage in ("ingesting", "queued"):
        # Row count unknown for this format: report rows done, not a fake bar.
        return {"percent": None, "basis": f"{job.rows_seen:,} records parsed", "determinate": False}
    return {
        "percent": round(start * 100, 1),
        "basis": {
            "source_verification": "verifying source hash and counting records",
            "graph_building": "building graph snapshot",
            "findings": "scoring deterministic findings",
            "ml_scoring": "running anomaly stack",
            "analytics": "clustering entities and correlating network observations",
        }.get(job.stage, job.stage),
        "determinate": True,
    }


def job_view(job: ImportJob, session=None) -> dict:
    from app.jobs.analysis import analysis_view

    return {
        "job_id": job.id,
        "total_records": job.total_records,
        "progress": job_progress(job),
        "case_id": job.case_id,
        "source_id": job.source_id,
        "state": job.state,
        "stage": job.stage,
        "attempt": job.attempt,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "bytes_read": job.bytes_read,
        "rows_seen": job.rows_seen,
        "rows_accepted": job.rows_accepted,
        "rows_quarantined": job.rows_quarantined,
        "snapshot_id": job.snapshot_id,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "analysis": analysis_view(session, job) if session is not None else None,
    }


def record_worker_heartbeat(session: Session, worker_id: str) -> None:
    heartbeat = session.get(WorkerHeartbeat, worker_id)
    if heartbeat is None:
        session.add(WorkerHeartbeat(worker_id=worker_id, heartbeat_at=utcnow()))
    else:
        heartbeat.heartbeat_at = utcnow()


def create_or_reuse_job(
    session: Session, *, case_id: str, source: EvidenceSource, idempotency_key: str, actor: User
) -> tuple[ImportJob, bool]:
    existing = session.scalar(
        select(ImportJob).where(ImportJob.case_id == case_id, ImportJob.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.source_id != source.id:
            raise ValueError("Idempotency-Key was already used for a different source")
        return existing, True
    job = ImportJob(
        case_id=case_id, source_id=source.id, idempotency_key=idempotency_key, state="queued", stage="queued"
    )
    session.add(job)
    session.flush()
    append_event(
        session, case_id=case_id, event_type="import.queued", stage="queued", job=job, payload={"source_id": source.id}
    )
    session.add(
        AuditRecord(
            case_id=case_id,
            actor_id=actor.id,
            action="import.queued",
            target_type="import_job",
            target_id=job.id,
            detail={"source_id": source.id, "sha256": source.sha256},
        )
    )
    return job, False


def claim_next_job(session: Session, *, worker_id: str, lease_seconds: int) -> ImportJob | None:
    now = utcnow()
    query = (
        select(ImportJob)
        .where(
            or_(
                ImportJob.state == "queued",
                (ImportJob.state.in_({"running", "checkpointed"}) & (ImportJob.lease_expires_at < now)),
            )
        )
        .order_by(ImportJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = session.scalar(query)
    if not job:
        return None
    reclaimed = job.state in {"running", "checkpointed"}
    job.state = "running"
    job.stage = "source_verification"
    job.lease_owner = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.attempt += 1
    job.started_at = job.started_at or now
    append_event(
        session,
        case_id=job.case_id,
        event_type="import.reclaimed" if reclaimed else "import.started",
        stage=job.stage,
        job=job,
        payload={"attempt": job.attempt, "worker_id": worker_id},
    )
    return job


def complete_job(session: Session, *, job: ImportJob, byte_size: int) -> None:
    job.state = "completed"
    job.stage = "source_verified"
    job.bytes_read = byte_size
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = utcnow()
    append_event(
        session,
        case_id=job.case_id,
        event_type="import.completed",
        stage=job.stage,
        job=job,
        payload={"foundation_only": True, "bytes_read": byte_size},
    )


def fail_job(session: Session, *, job: ImportJob, code: str, detail: str) -> None:
    from app.jobs.analysis import fail_running_stage

    fail_running_stage(session, job, detail)
    job.state = "failed"
    job.stage = "failed"
    job.error_code = code
    job.error_detail = detail[:2000]
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = utcnow()
    append_event(
        session, case_id=job.case_id, event_type="import.failed", stage="failed", job=job, payload={"error_code": code}
    )
