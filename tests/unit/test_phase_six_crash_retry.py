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

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine.catalogue import fragments as fragments_module
from app.engine.ingestion import pipeline as pipeline_module
from app.main import app
from app.models import FragmentReceipt, ImportJob
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


def test_worker_retries_transient_deadlock_before_failing_job(tmp_path: Path, monkeypatch) -> None:
    client, sessions, settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "phase6-deadlock"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Phase 6 deadlock", "synthetic": True}).json()["case_id"]
        _upload(client, case_id=case_id, headers=headers, key="deadlock-retry")

        calls = {"count": 0}

        def flaky_ingest(session, settings, job, source):
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlalchemy.exc.OperationalError(
                    "deadlock detected",
                    None,
                    None,
                    orig=RuntimeError("simulated deadlock"),
                )

        monkeypatch.setattr(runner, "ingest_source", flaky_ingest)
        assert runner.process_one("worker-deadlock") is True
        assert calls["count"] == 2, "deadlock retry should reattempt the job before reporting failure"

        with sessions() as session:
            job = session.scalar(
                select(ImportJob).where(ImportJob.case_id == case_id).order_by(ImportJob.created_at.desc())
            )
            assert job.state == "queued" or job.state == "running", job.state
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
