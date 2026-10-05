"""Run durable imports with immutable source verification and bounded DB retries."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import socket
import sqlite3
import time
from datetime import timedelta

from psycopg import Error as PsycopgError
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import SessionLocal, ensure_schema
from app.engine.ingestion import ingest_source
from app.events import append_event
from app.jobs.analysis import StageTracker, fail_running_stage
from app.jobs.grouping import run_grouping_only
from app.jobs.service import claim_next_job, fail_job, record_worker_heartbeat, utcnow
from app.models import EvidenceSource, ImportJob
from app.resources import current_plan
from app.storage.raw import resolve_source


def _is_retryable_database_error(exc: BaseException) -> bool:
    """Use driver codes, never SQL/parameter text or a poisoned-session message."""
    if not isinstance(exc, (DBAPIError, PsycopgError, sqlite3.Error)):
        return False
    # COPY and other direct driver calls bypass SQLAlchemy's exception wrapper.
    original = exc.orig if isinstance(exc, DBAPIError) else exc
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate is not None:
        # PostgreSQL: deadlock, serialization failure, or lock unavailable.
        return sqlstate in {"40P01", "40001", "55P03"}
    if isinstance(original, sqlite3.Error):
        code = getattr(original, "sqlite_errorcode", None)
        # Extended SQLite codes (e.g. BUSY_SNAPSHOT) share the primary low byte.
        return isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return False


def process_one(worker_id: str) -> bool:
    with SessionLocal() as session:
        record_worker_heartbeat(session, worker_id)
        job = claim_next_job(session, worker_id=worker_id, lease_seconds=settings.lease_seconds)
        if job is not None and job.error_code == "DB_DEADLOCK_RETRY":
            # Compatibility with the earlier retry patch: this is historical
            # retry information, not an outstanding failure on the new attempt.
            append_event(session, case_id=job.case_id, event_type="import.retry_recovered", stage=job.stage,
                         job=job, payload={"prior_error_code": job.error_code, "reason": job.error_detail,
                                          "attempt": job.attempt, "worker_id": worker_id})
            job.error_code = job.error_detail = None
        session.commit()
        if job is None:
            return False
        # Rollback expires ORM attributes, so keep immutable identities outside
        # the transaction. Retry this job, not the next item in the global queue.
        job_id, source_id = job.id, job.source_id
        for attempt in range(2):
            try:
                source = session.get(EvidenceSource, source_id)
                verification = StageTracker(session, job)
                job.stage = "source_verification"
                verification.start("source_verification")
                if source is None:
                    raise RuntimeError("source record missing")
                path = resolve_source(settings.evidence_root, source.storage_relative_path)
                if not path.is_file():
                    raise RuntimeError("immutable source file missing")
                with path.open("rb") as handle:
                    actual = hashlib.file_digest(handle, "sha256").hexdigest()
                if actual != source.sha256:
                    raise RuntimeError("immutable source hash mismatch")
                if path.stat().st_size != source.byte_size:
                    raise RuntimeError("immutable source byte-size mismatch")
                verification.finish(details={"sha256": actual, "byte_size": source.byte_size})
                if run_grouping_only(session, job=job, evidence_root=settings.evidence_root):
                    return True
                ingest_source(session, settings=settings, job=job, source=source)
                return True
            except Exception as exc:  # every worker failure must be recorded durably.
                session.rollback()
                job = session.get(ImportJob, job_id, populate_existing=True, with_for_update=True)
                if job is None:
                    logging.getLogger(__name__).exception("job %s disappeared during failure recovery", job_id)
                    return True
                if job.lease_owner != worker_id or job.state not in {"running", "checkpointed"}:
                    # A replacement worker or completed job must never be
                    # overwritten by this worker's stale failure/retry handler.
                    logging.getLogger(__name__).warning("job %s is no longer owned by %s", job_id, worker_id)
                    session.rollback()
                    return True
                if _is_retryable_database_error(exc) and attempt == 0:
                    failed_stage = job.stage
                    fail_running_stage(session, job, str(exc))
                    job.attempt += 1
                    job.state = "running"
                    job.stage = "source_verification"
                    job.lease_expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
                    job.error_code = job.error_detail = None
                    # Persist retry history without presenting it as a current
                    # terminal error. Retain the lease and committed receipts.
                    append_event(session, case_id=job.case_id, event_type="import.retrying", stage=failed_stage,
                                 job=job, payload={"attempt": job.attempt, "worker_id": worker_id,
                                                  "reason": str(exc)[:2000], "max_automatic_retries": 1,
                                                  "resume_from_receipts": True})
                    record_worker_heartbeat(session, worker_id)
                    session.commit()
                    logging.getLogger(__name__).warning(
                        "job %s hit a transient database lock; retrying once: %s",
                        job_id,
                        exc,
                    )
                    time.sleep(0.1)
                    continue
                logging.getLogger(__name__).exception("job %s failed at %s", job_id, job.stage)
                code = "SOURCE_VERIFICATION_FAILED" if job.stage == "source_verification" else "ANALYSIS_STAGE_FAILED"
                fail_job(session, job=job, code=code, detail=str(exc))
                session.commit()
                return True
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim/process at most one job and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-phase1")
    parser.add_argument("--profile-output", help="save cumulative parent-worker profile; child CPU is not included")
    args = parser.parse_args()
    # Stage-level progress (e.g. the bounded findings' per-section timings) at INFO.
    logging.basicConfig(level=os.environ.get("TRACEX_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Bring an older database forward (e.g. import_jobs.total_records) before the
    # first claim query selects every ImportJob column.
    ensure_schema()
    plan = current_plan()
    print(
        f"tracex-worker {args.worker_id}: {plan.cpu_count} CPU(s), "
        f"{(plan.available_memory_bytes or 0) >> 20} MB available, budget {plan.memory_budget_bytes >> 20} MB, "
        f"insert chunk {plan.insert_chunk_rows} rows, parse batch <= {plan.max_ingestion_batch_records} records",
        flush=True,
    )
    profile = None
    if args.profile_output:
        import cProfile
        from pathlib import Path

        if Path(args.profile_output).exists():
            parser.error("profile output exists; choose a new path to preserve prior evidence")
        profile = cProfile.Profile()
    while True:
        if args.profile_output:
            processed = profile.runcall(process_one, args.worker_id)
            if processed:
                profile.dump_stats(args.profile_output)
        else:
            processed = process_one(args.worker_id)
        if args.once:
            return
        if not processed:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
