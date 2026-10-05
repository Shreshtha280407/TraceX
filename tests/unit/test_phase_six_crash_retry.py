"""Phase 6 crash-and-retry evidence.

A hard process kill is modelled as an unhandled `KeyboardInterrupt` raised
inside the real write path, at the exact point the guide names: mid-fragment
(after the immutable Parquet bytes are written and fsynced, but before the
atomic rename) and after-rename-but-before-receipt (the fragment file is
already durably on disk, but the DB transaction that would record its
receipt never commits). `KeyboardInterrupt` is deliberately not `Exception`,
so it is not swallowed by `workers.runner.process_one`'s
`except Exception as exc: ... fail_job(...)` handler -- exactly like a real
SIGKILL, the job is left exactly where the crash found it, not marked
"failed" by application code that never ran.

Retry is then a second `runner.process_one()` call after reclaiming the lease
(a fresh worker would do this once the original lease expires; the test
forces that by moving `lease_expires_at` into the past, the same effect
`claim_next_job`'s `lease_expires_at < now` branch relies on in production).
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine.catalogue import fragments as fragments_module
from app.engine.ingestion import pipeline as pipeline_module
from app.main import app
from app.models import AnalysisStage, CaseEvent, FindingConfidence, FindingRecord, FragmentReceipt, ImportJob
from workers import runner

TXID_A = "a" * 64
TXID_B = "b" * 64
TXID_C = "c" * 64
TXID_D = "d" * 64
ROW_COUNT = 4


def _rows() -> bytes:
    # A->B->C->D: three verified, strictly-decreasing UTXO hops, meeting
    # `detect_peeling_chains`'s `DEFAULT_MIN_PEELING_LENGTH = 3` so the
    # post-retry findings assertion below exercises a real detector, not just
    # the address-window rules.
    rows = [
        {
            "txid": TXID_A,
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:00:00Z",
            "inputs": [],
            "outputs": [{"address": "addr-a", "amount_sats": 100_000}],
            "fee_sats": 0,
        },
        {
            "txid": TXID_B,
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:01:00Z",
            "inputs": [{"prev_txid": TXID_A, "prev_vout": 0, "address": "addr-a", "amount_sats": 100_000}],
            "outputs": [{"address": "addr-b", "amount_sats": 99_000}],
            "fee_sats": 1_000,
        },
        {
            "txid": TXID_C,
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:02:00Z",
            "inputs": [{"prev_txid": TXID_B, "prev_vout": 0, "address": "addr-b", "amount_sats": 99_000}],
            "outputs": [{"address": "addr-c", "amount_sats": 98_000}],
            "fee_sats": 1_000,
        },
        {
            "txid": TXID_D,
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:03:00Z",
            "inputs": [{"prev_txid": TXID_C, "prev_vout": 0, "address": "addr-c", "amount_sats": 98_000}],
            "outputs": [{"address": "addr-d", "amount_sats": 97_000}],
            "fee_sats": 1_000,
        },
    ]
    import json

    return "\n".join(json.dumps(row) for row in rows).encode()


def _test_app(tmp_path: Path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'control.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        evidence_root=tmp_path / "evidence",
        max_upload_bytes=1024 * 1024,
        lease_seconds=30,
        event_heartbeat_seconds=1,
    )

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    return TestClient(app), sessions, settings


def _expire_lease(sessions, job_id: str) -> None:
    """Simulate enough wall-clock time passing for a fresh worker to reclaim the lease."""
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def _upload(client: TestClient, *, case_id: str, headers: dict, key: str) -> tuple[str, bytes]:
    payload = _rows()
    response = client.post(
        f"/v1/cases/{case_id}/imports",
        headers={**headers, "Idempotency-Key": key},
        files={"file": ("chain.ndjson", payload, "application/x-ndjson")},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"], payload


def _sqlite_lock_error(code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize("original", [
    pg_errors.DeadlockDetected("deadlock detected"),
    pg_errors.SerializationFailure("could not serialize access due to concurrent update"),
    pg_errors.LockNotAvailable("could not obtain lock on row"),
    _sqlite_lock_error(sqlite3.SQLITE_BUSY),
    _sqlite_lock_error(sqlite3.SQLITE_LOCKED),
    _sqlite_lock_error(sqlite3.SQLITE_BUSY_SNAPSHOT),
], ids=["postgres-deadlock", "postgres-serialization", "postgres-lock", "sqlite-busy", "sqlite-locked",
        "sqlite-busy-snapshot"])
@pytest.mark.parametrize("wrapped", [True, False], ids=["sqlalchemy", "direct-driver"])
def test_retry_classifier_uses_driver_codes(original, wrapped) -> None:
    error = sqlalchemy.exc.OperationalError("UPDATE import_jobs SET stage=:stage", None, original) if wrapped else original
    assert runner._is_retryable_database_error(error)


@pytest.mark.parametrize("error", [
    sqlalchemy.exc.OperationalError("SELECT * FROM import_jobs FOR UPDATE SKIP LOCKED", None,
                                    pg_errors.ConnectionFailure("connection lost")),
    sqlalchemy.exc.OperationalError("SELECT * FROM blockchain_transactions", {"label": "deadlock"},
                                    RuntimeError("connection lost")),
    sqlalchemy.exc.OperationalError("UPDATE import_jobs SET stage=:stage", None,
                                    RuntimeError("simulated deadlock")),
    sqlalchemy.exc.OperationalError("UPDATE import_jobs SET stage=:stage", None,
                                    sqlite3.OperationalError("database is locked")),
    sqlalchemy.exc.OperationalError("UPDATE import_jobs SET stage=:stage", None,
                                    _sqlite_lock_error(sqlite3.SQLITE_IOERR)),
    sqlalchemy.exc.IntegrityError("INSERT INTO lock_table VALUES (:label)", {"label": "deadlock"},
                                  pg_errors.UniqueViolation("duplicate key")),
    sqlalchemy.exc.PendingRollbackError("rollback required after deadlock"),
    RuntimeError("deadlock detected"),
    pg_errors.ConnectionFailure("connection lost"),
    _sqlite_lock_error(sqlite3.SQLITE_IOERR),
], ids=["connection", "sql-and-parameters", "untyped-driver-message", "sqlite-missing-code", "sqlite-io",
        "integrity", "pending-rollback", "not-dbapi", "direct-connection", "direct-sqlite-io"])
def test_retry_classifier_never_uses_sql_parameters_or_error_wording(error) -> None:
    assert not runner._is_retryable_database_error(error)


def test_retry_classifier_supports_legacy_postgresql_driver_codes() -> None:
    class LegacyDriverError(Exception):
        pgcode = "40001"

    error = sqlalchemy.exc.OperationalError("UPDATE import_jobs SET stage=:stage", None, LegacyDriverError())
    assert runner._is_retryable_database_error(error)


@pytest.mark.parametrize("driver_error", [pg_errors.DeadlockDetected, pg_errors.SerializationFailure])
@pytest.mark.parametrize("wrapped", [True, False], ids=["sqlalchemy", "direct-driver"])
def test_worker_retries_transient_deadlock_before_failing_job(
    tmp_path: Path, monkeypatch, driver_error, wrapped
) -> None:
    client, sessions, settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "phase6-deadlock"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Phase 6 deadlock", "synthetic": True}).json()["case_id"]
        job_id, payload = _upload(client, case_id=case_id, headers=headers, key="deadlock-retry")
        other_case = client.post("/v1/cases", headers=headers,
                                 json={"name": "Unrelated queued import", "synthetic": True}).json()["case_id"]
        other_job_id, _ = _upload(client, case_id=other_case, headers=headers, key="other-job")

        calls = {"count": 0, "claims": 0}
        before = {}
        original_ml = pipeline_module._materialize_ml_findings
        original_claim = runner.claim_next_job

        def claim(*args, **kwargs):
            calls["claims"] += 1
            return original_claim(*args, **kwargs)

        def flaky_ml(session, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                before["receipts"] = {r.id: r.sha256 for r in session.scalars(
                    select(FragmentReceipt).where(FragmentReceipt.job_id == job_id))}
                before["pins"] = copy.deepcopy({p.finding_id: p.provenance for p in session.scalars(
                    select(FindingConfidence).where(FindingConfidence.case_id == case_id))})
                before["stages"] = {s.id: (s.status, s.duration_seconds, copy.deepcopy(s.details))
                    for s in session.scalars(select(AnalysisStage).where(
                        AnalysisStage.job_id == job_id, AnalysisStage.status == "complete"))}
                finding = session.scalar(select(FindingRecord).where(FindingRecord.case_id == case_id))
                assert finding is not None and before["pins"] and before["receipts"]
                before["reviewed_finding"] = finding.id
                review = client.post(f"/v1/findings/{finding.id}/reviews", headers=headers, json={
                    "expected_finding_version": 1, "disposition": "dismissed",
                    "reason": "Preserve the investigator's bounded proposition decision on retry"})
                assert review.status_code == 201, review.text
                error = driver_error("injected transient database conflict")
                if not wrapped:
                    raise error
                raise sqlalchemy.exc.OperationalError(
                    "UPDATE import_jobs SET stage=:stage",
                    {"stage": "ml_scoring"},
                    error,
                )
            return original_ml(session, **kwargs)

        monkeypatch.setattr(runner, "claim_next_job", claim)
        monkeypatch.setattr(pipeline_module, "_materialize_ml_findings", flaky_ml)
        assert runner.process_one("worker-deadlock") is True
        assert calls["count"] == 2, "deadlock retry should reattempt the job before reporting failure"
        assert calls["claims"] == 1, "automatic retry must retain its job and lease, not claim from the global queue"

        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "completed" and job.attempt == 2
            assert job.error_code is None and job.error_detail is None
            assert job.rows_seen == job.rows_accepted == ROW_COUNT and job.rows_quarantined == 0
            assert session.get(ImportJob, other_job_id).state == "queued"
            assert {r.id: r.sha256 for r in session.scalars(select(FragmentReceipt).where(
                FragmentReceipt.job_id == job_id))} == before["receipts"]
            for finding_id, provenance in before["pins"].items():
                assert session.get(FindingConfidence, finding_id).provenance == provenance
            for stage_id, state in before["stages"].items():
                stage = session.get(AnalysisStage, stage_id)
                assert (stage.status, stage.duration_seconds, stage.details) == state
            reviewed = session.get(FindingRecord, before["reviewed_finding"])
            assert reviewed.status == "dismissed" and reviewed.finding_version == 2
            interrupted = session.scalar(select(AnalysisStage).where(
                AnalysisStage.job_id == job_id, AnalysisStage.name == "ml_scoring", AnalysisStage.attempt == 1))
            assert interrupted.status == "failed" and interrupted.completed_at is not None
            retry = session.scalar(select(CaseEvent).where(CaseEvent.job_id == job_id,
                                                           CaseEvent.event_type == "import.retrying"))
            assert retry.payload["attempt"] == 2 and retry.stage == "ml_scoring"
            assert retry.payload["resume_from_receipts"] is True
        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        assert job["error_code"] is None and job["error_detail"] is None
        # Four transactions do not establish ML adequacy: retry must not convert
        # insufficient data or missing Geo-IP coverage into a claimed full pass.
        assert job["analysis"]["state"] == "degraded"
        assert any(s["name"] == "ml_scoring" and s["attempt"] == 2 and s["status"] == "insufficient_data"
                   for s in job["analysis"]["stages"])
        queue = client.get(f"/v1/cases/{case_id}/investigation-queue", headers=headers)
        assert queue.status_code == 200 and queue.json()["grouping_coverage"]["state"] == "complete"
        raw_path = settings.evidence_root / case_id / __import__("hashlib").sha256(payload).hexdigest() / "original"
        assert raw_path.read_bytes() == payload
    finally:
        app.dependency_overrides.clear()


def test_worker_stops_after_one_automatic_retry_and_records_terminal_failure(tmp_path: Path, monkeypatch) -> None:
    client, sessions, _settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "retry-limit"}
    try:
        case = client.post("/v1/cases", headers=headers, json={"name": "Retry limit"}).json()["case_id"]
        job_id, _ = _upload(client, case_id=case, headers=headers, key="retry-limit")
        calls = []

        def broken(session, *, settings, job, source):
            from app.jobs.analysis import StageTracker

            calls.append((job.id, job.attempt, job.lease_owner))
            job.stage = "ingesting"
            StageTracker(session, job).start("ingesting")
            raise sqlalchemy.exc.OperationalError("INSERT INTO transactions VALUES (:txid)", None,
                                                   pg_errors.DeadlockDetected("persistent deadlock"))

        monkeypatch.setattr(runner, "ingest_source", broken)
        assert runner.process_one("limited-worker")
        assert calls == [(job_id, 1, "limited-worker"), (job_id, 2, "limited-worker")]
        assert runner.process_one("limited-worker") is False
        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "failed" and job.error_code == "ANALYSIS_STAGE_FAILED"
            assert "persistent deadlock" in job.error_detail
            assert job.lease_owner is None and job.lease_expires_at is None
            stages = list(session.scalars(select(AnalysisStage).where(
                AnalysisStage.job_id == job_id, AnalysisStage.name == "ingesting").order_by(AnalysisStage.attempt)))
            assert [s.status for s in stages] == ["failed", "failed"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("error", [
    sqlalchemy.exc.OperationalError("SELECT * FROM import_jobs FOR UPDATE SKIP LOCKED", None,
                                    pg_errors.ConnectionFailure("connection lost")),
    sqlalchemy.exc.PendingRollbackError("rollback required after deadlock"),
    RuntimeError("immutable source hash mismatch"),
])
def test_worker_does_not_retry_unrelated_failures(tmp_path: Path, monkeypatch, error) -> None:
    client, sessions, _settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "not-retryable"}
    try:
        case = client.post("/v1/cases", headers=headers, json={"name": "Not retryable"}).json()["case_id"]
        job_id, _ = _upload(client, case_id=case, headers=headers, key="not-retryable")
        calls = []

        def broken(*args, **kwargs):
            calls.append(kwargs["job"].id)
            raise error

        monkeypatch.setattr(runner, "ingest_source", broken)
        assert runner.process_one("nonretry-worker")
        assert calls == [job_id]
        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "failed" and job.attempt == 1
            assert session.scalar(select(CaseEvent).where(CaseEvent.job_id == job_id,
                                                         CaseEvent.event_type == "import.retrying")) is None
    finally:
        app.dependency_overrides.clear()


def test_worker_source_hash_failure_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    client, sessions, settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "source-integrity"}
    try:
        case = client.post("/v1/cases", headers=headers, json={"name": "Source integrity"}).json()["case_id"]
        job_id, payload = _upload(client, case_id=case, headers=headers, key="source-integrity")
        path = settings.evidence_root / case / __import__("hashlib").sha256(payload).hexdigest() / "original"
        path.write_bytes(payload + b"\n")  # Corrupt only this test's isolated temporary fixture.

        def must_not_ingest(*args, **kwargs):
            pytest.fail("source verification failure must never reach ingestion")

        monkeypatch.setattr(runner, "ingest_source", must_not_ingest)
        assert runner.process_one("integrity-worker")
        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "failed" and job.attempt == 1 and job.error_code == "SOURCE_VERIFICATION_FAILED"
            assert "immutable source hash mismatch" in job.error_detail
            assert session.scalar(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id)) is None
            assert session.scalar(select(CaseEvent).where(CaseEvent.job_id == job_id,
                                                         CaseEvent.event_type == "import.retrying")) is None
    finally:
        app.dependency_overrides.clear()


def test_worker_does_not_overwrite_a_replacement_workers_lease(tmp_path: Path, monkeypatch) -> None:
    client, sessions, _settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "lease-race"}
    try:
        case = client.post("/v1/cases", headers=headers, json={"name": "Lease race"}).json()["case_id"]
        job_id, _ = _upload(client, case_id=case, headers=headers, key="lease-race")

        def reclaimed(*args, **kwargs):
            with sessions() as session:
                job = session.get(ImportJob, job_id)
                job.lease_owner = "replacement-worker"
                job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
                session.commit()
            raise sqlalchemy.exc.OperationalError("UPDATE import_jobs SET stage=:stage", None,
                                                   pg_errors.DeadlockDetected("stale worker conflict"))

        monkeypatch.setattr(runner, "ingest_source", reclaimed)
        assert runner.process_one("stale-worker")
        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "running" and job.lease_owner == "replacement-worker" and job.attempt == 1
            assert job.error_code is None
            assert session.scalar(select(CaseEvent).where(CaseEvent.job_id == job_id,
                                                         CaseEvent.event_type == "import.retrying")) is None
    finally:
        app.dependency_overrides.clear()


def test_successful_import_clears_legacy_transient_retry_error(tmp_path: Path, monkeypatch) -> None:
    client, sessions, _settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "legacy-retry"}
    try:
        case = client.post("/v1/cases", headers=headers, json={"name": "Legacy retry"}).json()["case_id"]
        job_id, _ = _upload(client, case_id=case, headers=headers, key="legacy-retry")
        with sessions() as session:
            job = session.get(ImportJob, job_id)
            job.error_code = "DB_DEADLOCK_RETRY"
            job.error_detail = "historical transient lock, not a terminal error"
            session.commit()
        assert runner.process_one("legacy-worker")
        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        assert job["state"] == "completed" and job["error_code"] is None and job["error_detail"] is None
        with sessions() as session:
            event = session.scalar(select(CaseEvent).where(CaseEvent.job_id == job_id,
                                                          CaseEvent.event_type == "import.retry_recovered"))
            assert event.payload["prior_error_code"] == "DB_DEADLOCK_RETRY"
            assert event.payload["reason"] == "historical transient lock, not a terminal error"
    finally:
        app.dependency_overrides.clear()


def test_kill_mid_fragment_write_before_atomic_rename_then_retry_reaches_identical_canonical_counts(
    tmp_path: Path, monkeypatch
) -> None:
    client, sessions, settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "phase6-crash"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Phase 6 crash A", "synthetic": True}).json()["case_id"]
        job_id, payload = _upload(client, case_id=case_id, headers=headers, key="crash-mid-fragment")

        real_replace = fragments_module.os.replace
        crashed = {"done": False}

        def crash_before_rename(src, dst, *a, **k):
            if not crashed["done"] and str(dst).endswith("transactions.parquet"):
                crashed["done"] = True
                # The parquet bytes are already flushed+fsynced to `src` at this
                # point (see `_write_immutable`); only the rename is interrupted.
                raise KeyboardInterrupt("simulated hard kill before atomic rename")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(fragments_module.os, "replace", crash_before_rename)
        with pytest.raises(KeyboardInterrupt):
            runner.process_one("worker-before-crash")
        monkeypatch.setattr(fragments_module.os, "replace", real_replace)

        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "running", "a crash before the batch commits must not silently mark the job checkpointed"
            receipts = list(session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id)))
            assert receipts == [], "no fragment receipt may exist for a fragment that was never durably renamed"

        fragment_path = settings.evidence_root / case_id / "derived" / __import__("hashlib").sha256(payload).hexdigest() / "batch-00000000" / "transactions.parquet"
        assert not fragment_path.exists(), "the interrupted rename must leave no final fragment behind"
        staging_dir = fragment_path.parent / ".staging"
        if staging_dir.exists():
            assert list(staging_dir.glob("*.part")) == [], "the .part temp file must be cleaned up even on a raised exception (the `finally: unlink`)"

        _expire_lease(sessions, job_id)
        assert runner.process_one("worker-after-crash") is True

        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        assert job["state"] == "completed", job
        assert job["rows_seen"] == ROW_COUNT
        assert job["rows_accepted"] == ROW_COUNT
        assert job["rows_quarantined"] == 0

        with sessions() as session:
            receipts = list(session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id, FragmentReceipt.record_type == "transactions")))
            assert len(receipts) == 1, "retry must not create a duplicate receipt for the same logical batch"
            table = pq.read_table(settings.evidence_root / receipts[0].storage_relative_path)
            assert table.num_rows == ROW_COUNT, "retry must not duplicate rows into the recovered fragment"

        findings = client.get(f"/v1/cases/{case_id}/findings", headers=headers).json()["findings"]
        assert any(f["rule_id"] == "peeling_chain_candidate" for f in findings)
    finally:
        app.dependency_overrides.clear()


def test_kill_after_rename_before_receipt_commit_then_retry_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    client, sessions, settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "phase6-crash"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Phase 6 crash B", "synthetic": True}).json()["case_id"]
        job_id, payload = _upload(client, case_id=case_id, headers=headers, key="crash-after-rename")

        real_append_event = pipeline_module.append_event
        crashed = {"done": False}

        def crash_after_publish(*a, **k):
            # `_commit_batch` calls `publish_batch` (fragments are already
            # durably renamed to their final immutable path), builds the
            # FragmentReceipt rows in-session, then calls `append_event`
            # immediately before `session.commit()`. Raising here means the
            # receipts were `session.add()`-ed but the transaction never
            # committed -- exactly "after rename, before receipt".
            if not crashed["done"]:
                crashed["done"] = True
                raise KeyboardInterrupt("simulated hard kill after fragment rename, before receipt commit")
            return real_append_event(*a, **k)

        monkeypatch.setattr(pipeline_module, "append_event", crash_after_publish)
        with pytest.raises(KeyboardInterrupt):
            runner.process_one("worker-before-crash")
        monkeypatch.setattr(pipeline_module, "append_event", real_append_event)

        fragment_path = settings.evidence_root / case_id / "derived" / __import__("hashlib").sha256(payload).hexdigest() / "batch-00000000" / "transactions.parquet"
        assert fragment_path.is_file(), "the fragment must already be durably on disk before the crash point"
        fragment_bytes_before_retry = fragment_path.read_bytes()

        with sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job.state == "running"
            receipts = list(session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id)))
            assert receipts == [], "the uncommitted receipt insert must not be visible after the crash"

        _expire_lease(sessions, job_id)
        assert runner.process_one("worker-after-crash") is True

        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        assert job["state"] == "completed", job
        assert (job["rows_seen"], job["rows_accepted"], job["rows_quarantined"]) == (ROW_COUNT, ROW_COUNT, 0)

        assert fragment_path.read_bytes() == fragment_bytes_before_retry, "retry must reuse the byte-identical fragment already on disk, not rewrite it"
        with sessions() as session:
            receipts = list(session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id, FragmentReceipt.record_type == "transactions")))
            assert len(receipts) == 1
            table = pq.read_table(settings.evidence_root / receipts[0].storage_relative_path)
            assert table.num_rows == ROW_COUNT
    finally:
        app.dependency_overrides.clear()
