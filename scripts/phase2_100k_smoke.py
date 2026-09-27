"""Run the Phase 2 100K clean NDJSON gate in a disposable local control/evidence store."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.db import Base, make_engine
from app.jobs.service import create_or_reuse_job
from app.models import Case, CaseMembership, EvidenceSource, FragmentReceipt, GraphSnapshot, User
from workers import runner


def main() -> None:
    source_path = Path(__file__).resolve().parents[1] / "fixtures" / "demo_100k" / "ingestion_rows.ndjson"
    if not source_path.is_file():
        raise SystemExit("Generate fixtures first: python3 fixtures/demo_100k/generate.py")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="tracex-phase2-100k-") as temporary:
        root = Path(temporary)
        evidence_root = root / "evidence"
        engine = make_engine(f"sqlite:///{root / 'control.db'}")
        Base.metadata.create_all(engine)
        from sqlalchemy.orm import sessionmaker

        sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with sessions() as session:
            user = User(external_subject="phase2-smoke")
            session.add(user)
            session.flush()
            case = Case(name="Phase 2 100K synthetic gate", synthetic=True, created_by=user.id)
            session.add(case)
            session.flush()
            session.add(CaseMembership(case_id=case.id, user_id=user.id, role="case_lead"))
            relative = Path(case.id) / source_hash / "original"
            destination = evidence_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)
            source = EvidenceSource(
                case_id=case.id,
                sha256=source_hash,
                byte_size=source_path.stat().st_size,
                original_filename=source_path.name,
                source_format="ndjson",
                storage_relative_path=relative.as_posix(),
                synthetic=True,
            )
            session.add(source)
            session.flush()
            job, _ = create_or_reuse_job(
                session, case_id=case.id, source=source, idempotency_key="phase2-100k-gate", actor=user
            )
            session.commit()
            job_id = job.id
        settings = Settings(
            database_url=f"sqlite:///{root / 'control.db'}",
            evidence_root=evidence_root,
            max_upload_bytes=source_path.stat().st_size + 1,
            lease_seconds=120,
            event_heartbeat_seconds=1,
            ingestion_batch_records=32768,
        )
        old_settings, old_session = runner.settings, runner.SessionLocal
        runner.settings, runner.SessionLocal = settings, sessions
        started = time.perf_counter()
        try:
            if not runner.process_one("phase2-100k-worker"):
                raise RuntimeError("worker did not claim the 100K job")
        finally:
            runner.settings, runner.SessionLocal = old_settings, old_session
        elapsed = time.perf_counter() - started
        with sessions() as session:
            from app.models import ImportJob

            job = session.get(ImportJob, job_id)
            receipts = session.query(FragmentReceipt).filter_by(job_id=job_id).all()
            graph = session.query(GraphSnapshot).filter_by(case_id=job.case_id).one_or_none() if job else None
            if (
                job is None
                or job.state != "completed"
                or (job.rows_seen, job.rows_accepted, job.rows_quarantined) != (100000, 100000, 0)
            ):
                raise RuntimeError(f"100K gate failed: {job_view(job) if job else 'job missing'}")
            if sum(receipt.record_count for receipt in receipts if receipt.record_type == "transactions") != 100000:
                raise RuntimeError("100K gate transaction fragment count mismatch")
            if graph is None or graph.node_count < 100000 or graph.edge_count < 200000:
                raise RuntimeError("100K gate graph snapshot is incomplete")
        print(f"Phase 2/3 100K gate passed in {elapsed:.2f}s")


def job_view(job) -> dict:
    return {
        "state": job.state,
        "rows_seen": job.rows_seen,
        "rows_accepted": job.rows_accepted,
        "rows_quarantined": job.rows_quarantined,
    }


if __name__ == "__main__":
    main()
