"""Phase 1 worker: claims a durable job and verifies immutable source bytes only."""

from __future__ import annotations

import argparse
import hashlib
import socket
import time

from app.config import settings
from app.db import SessionLocal, ensure_schema
from app.engine.ingestion import ingest_source
from app.jobs.service import claim_next_job, fail_job, record_worker_heartbeat
from app.models import EvidenceSource
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
            ingest_source(session, settings=settings, job=job, source=source)
            return True
        except Exception as exc:  # noqa: BLE001 - every worker failure must be recorded durably.
            session.rollback()
            job = session.get(type(job), job.id)
            if job is not None:
                fail_job(session, job=job, code="SOURCE_VERIFICATION_FAILED", detail=str(exc))
                session.commit()
            return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim/process at most one job and exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-phase1")
    args = parser.parse_args()
    # Bring an older database forward (e.g. import_jobs.total_records) before the
    # first claim query selects every ImportJob column.
    ensure_schema()
    while True:
        processed = process_one(args.worker_id)
        if args.once:
            return
        if not processed:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
