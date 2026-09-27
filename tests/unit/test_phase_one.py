from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app
from app.models import ImportJob, OutboxEvent
from workers import runner


def test_case_scoped_upload_worker_restart_and_event_replay(tmp_path: Path, monkeypatch) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'control.db'}")
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    test_settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        evidence_root=tmp_path / "evidence",
        max_upload_bytes=1024 * 1024,
        lease_seconds=1,
        event_heartbeat_seconds=1,
    )

    def override_session():
        session = test_sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", test_settings)
    monkeypatch.setattr(runner, "settings", test_settings)
    monkeypatch.setattr(runner, "SessionLocal", test_sessions)
    client = TestClient(app)
    owner_headers = {"X-TraceX-Actor": "alice"}
    try:
        case_response = client.post("/v1/cases", headers=owner_headers, json={"name": "Case A", "synthetic": True})
        assert case_response.status_code == 201
        case_id = case_response.json()["case_id"]

        member_response = client.post(
            f"/v1/cases/{case_id}/members", headers=owner_headers, json={"actor": "bob", "role": "analyst"}
        )
        assert member_response.status_code == 201

        payload = b"timestamp,txid\n2026-01-01T00:00:00Z,aaaaaaaa\n"
        upload = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**owner_headers, "Idempotency-Key": "case-a-tiny-upload"},
            files={"file": ("tiny.csv", payload, "text/csv")},
        )
        assert upload.status_code == 202, upload.text
        body = upload.json()
        source_path = test_settings.evidence_root / case_id / hashlib.sha256(payload).hexdigest() / "original"
        assert source_path.read_bytes() == payload

        forbidden = client.get(f"/v1/jobs/{body['job_id']}", headers={"X-TraceX-Actor": "mallory"})
        assert forbidden.status_code == 404
        visible = client.get(f"/v1/jobs/{body['job_id']}", headers={"X-TraceX-Actor": "bob"})
        assert visible.status_code == 200
        assert visible.json()["state"] == "queued"

        # Model a worker crash: a separate worker must reclaim an expired lease.
        with test_sessions() as session:
            job = session.get(ImportJob, body["job_id"])
            assert job is not None
            job.state = "running"
            job.stage = "source_verification"
            job.lease_owner = "crashed-worker"
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
            session.commit()
        assert runner.process_one("worker-after-restart")
        completed = client.get(f"/v1/jobs/{body['job_id']}", headers=owner_headers)
        assert completed.status_code == 200
        assert completed.json()["state"] == "completed"
        assert completed.json()["bytes_read"] == len(payload)
        assert client.get("/v1/readyz").status_code == 200
        with test_sessions() as session:
            assert session.query(OutboxEvent).filter_by(case_id=case_id).count() >= 3

        replay = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**owner_headers, "Idempotency-Key": "case-a-tiny-upload"},
            files={"file": ("tiny.csv", payload, "text/csv")},
        )
        assert replay.status_code == 202
        assert replay.json()["job_id"] == body["job_id"]
        assert replay.json()["idempotent_replay"] is True

        events = client.get(f"/v1/cases/{case_id}/events", headers=owner_headers)
        assert events.status_code == 200
        assert "import.queued" in events.text
        assert "import.reclaimed" in events.text
        assert "import.completed" in events.text
        latest = client.get(f"/v1/cases/{case_id}/events", headers={**owner_headers, "Last-Event-ID": "1"})
        assert "import.reclaimed" in latest.text
        assert "import.queued" not in latest.text
    finally:
        app.dependency_overrides.clear()


def test_unsupported_source_is_rejected_before_job_creation(tmp_path: Path, monkeypatch) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'control.db'}")
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    test_settings = Settings("sqlite://", tmp_path / "evidence", 1024, 1, 1)

    def override_session():
        with test_sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", test_settings)
    client = TestClient(app)
    try:
        headers = {"X-TraceX-Actor": "alice"}
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Case A"}).json()["case_id"]
        response = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**headers, "Idempotency-Key": "bad-file"},
            files={"file": ("payload.zip", b"x", "application/zip")},
        )
        assert response.status_code == 422
        with test_sessions() as session:
            assert session.query(ImportJob).count() == 0
    finally:
        app.dependency_overrides.clear()
