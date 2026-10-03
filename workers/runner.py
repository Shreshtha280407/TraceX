"""Phase 1 worker: claims a durable job and verifies immutable source bytes only."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import socket
import time

from app.config import settings
from app.db import SessionLocal, ensure_schema
from app.engine.ingestion import ingest_source
from app.jobs.service import claim_next_job, fail_job, record_worker_heartbeat
from app.models import EvidenceSource
from app.resources import current_plan
from app.storage.raw import resolve_source


def process_one(worker_id: str) -> bool:
    with SessionLocal() as session:
        record_worker_heartbeat(session, worker_id)
        job = claim_next_job(session, worker_id=worker_id, lease_seconds=settings.lease_seconds)
        session.commit()
        if job is None:
            return False
        source = session.get(EvidenceSource, job.source_id)
        try:
            from app.jobs.analysis import StageTracker

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
            ingest_source(session, settings=settings, job=job, source=source)
            return True
        except Exception as exc:  # every worker failure must be recorded durably.
            logging.getLogger(__name__).exception("job %s failed at %s", job.id, job.stage)
            session.rollback()
            job = session.get(type(job), job.id)
            if job is not None:
                code = "SOURCE_VERIFICATION_FAILED" if job.stage == "source_verification" else "ANALYSIS_STAGE_FAILED"
                fail_job(session, job=job, code=code, detail=str(exc))
                session.commit()
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
