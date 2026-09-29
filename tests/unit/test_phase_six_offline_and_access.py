"""Phase 6 offline-and-access evidence.

Two independent claims:

1. Ingestion (upload -> parse -> commit -> graph -> deterministic findings)
   makes no outbound network call. Proven by patching `socket.socket` itself
   to raise on construction for anything other than an AF_UNIX socket (which
   covers `TestClient`'s in-process ASGI transport and does not touch real
   sockets) -- if any code path under test opened a real TCP/UDP socket, this
   patch would raise inside it and the ingestion would fail.
2. A non-member (Case B) gets 404 -- not 403, matching the existing
   `require_case_member` policy of not revealing whether a case exists -- on
   every case-scoped read this phase cares about: the graph, the event
   stream, and both exports. `tests/unit/test_auth_and_case_reads.py` already
   covers case detail/sources; this file covers the endpoints Phase 6 calls
   out by name ("Case B cannot fetch Case A snapshots, event stream or
   exports").
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app
from workers import runner

TXID = "e" * 64


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    engine = make_engine(f"sqlite:///{tmp_path / 'offline.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'offline.db'}",
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
    return TestClient(app)


def test_full_ingestion_makes_no_outbound_network_socket(tmp_path: Path, monkeypatch) -> None:
    real_socket_init = socket.socket.__init__

    allowed_families = {socket.AF_UNIX} if hasattr(socket, "AF_UNIX") else set()

    def guarded_init(self, family=socket.AF_INET, type=socket.SOCK_STREAM, *a, **k):
        if family not in allowed_families:
            raise AssertionError(f"unexpected socket() call during offline ingestion: family={family!r} type={type!r}")
        return real_socket_init(self, family, type, *a, **k)

    monkeypatch.setattr(socket.socket, "__init__", guarded_init)

    client = _client(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "offline-analyst"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Offline case", "synthetic": True}).json()["case_id"]
        rows = [
            {
                "txid": TXID,
                "network": "bitcoin-regtest",
                "timestamp": "2026-01-01T00:00:00Z",
                "inputs": [],
                "outputs": [{"address": "addr-offline", "amount_sats": 50_000}],
                "fee_sats": 0,
            }
        ]
        payload = "\n".join(json.dumps(row) for row in rows).encode()
        upload = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**headers, "Idempotency-Key": "offline-ingest"},
            files={"file": ("rows.ndjson", payload, "application/x-ndjson")},
        )
        assert upload.status_code == 202, upload.text
        assert runner.process_one("offline-worker") is True

        job = client.get(f"/v1/jobs/{upload.json()['job_id']}", headers=headers).json()
        assert job["state"] == "completed", job

        graph = client.get(f"/v1/cases/{case_id}/graph", headers=headers, params={"seed": "addr-offline"})
        assert graph.status_code == 200
    finally:
        app.dependency_overrides.clear()


def _two_cases_with_case_a_populated(tmp_path: Path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_a = {"X-TraceX-Actor": "owner-a"}
    owner_b = {"X-TraceX-Actor": "owner-b"}
    case_a = client.post("/v1/cases", headers=owner_a, json={"name": "Case A", "synthetic": True}).json()["case_id"]
    case_b = client.post("/v1/cases", headers=owner_b, json={"name": "Case B", "synthetic": True}).json()["case_id"]
    rows = [
        {
            "txid": TXID,
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:00:00Z",
            "inputs": [],
            "outputs": [{"address": "addr-case-a", "amount_sats": 50_000}],
            "fee_sats": 0,
        }
    ]
    payload = "\n".join(json.dumps(row) for row in rows).encode()
    upload = client.post(
        f"/v1/cases/{case_a}/imports",
        headers={**owner_a, "Idempotency-Key": "case-a-ingest"},
        files={"file": ("rows.ndjson", payload, "application/x-ndjson")},
    )
    assert upload.status_code == 202, upload.text
    assert runner.process_one("cross-case-worker") is True
    job = client.get(f"/v1/jobs/{upload.json()['job_id']}", headers=owner_a).json()
    assert job["state"] == "completed", job
    return client, case_a, case_b, owner_a, owner_b


def test_case_b_cannot_fetch_case_a_graph_events_or_exports(tmp_path: Path, monkeypatch) -> None:
    client, case_a, case_b, owner_a, owner_b = _two_cases_with_case_a_populated(tmp_path, monkeypatch)
    try:
        # Sanity: the owner of Case A really does see real content at every one
        # of these endpoints (a 404 for everyone would trivially "pass" below).
        assert client.get(f"/v1/cases/{case_a}/graph", headers=owner_a, params={"seed": "addr-case-a"}).status_code == 200
        assert client.get(f"/v1/cases/{case_a}/events", headers=owner_a).status_code == 200
        assert client.get(f"/v1/cases/{case_a}/findings/export", headers=owner_a).status_code == 200
        assert client.get(f"/v1/cases/{case_a}/features/export", headers=owner_a).status_code == 200

        # Case B's owner is a real, authenticated user -- just not a member of Case A.
        graph = client.get(f"/v1/cases/{case_a}/graph", headers=owner_b, params={"seed": "addr-case-a"})
        events = client.get(f"/v1/cases/{case_a}/events", headers=owner_b)
        findings_export = client.get(f"/v1/cases/{case_a}/findings/export", headers=owner_b)
        features_export = client.get(f"/v1/cases/{case_a}/features/export", headers=owner_b)
        sources = client.get(f"/v1/cases/{case_a}/sources", headers=owner_b)

        for response in (graph, events, findings_export, features_export, sources):
            assert response.status_code == 404, response.text
            assert response.json() == {"detail": "Case not found"}, response.text

        # And membership in Case B does not leak Case B's own (empty) view into Case A's.
        assert client.get(f"/v1/cases/{case_b}/findings/export", headers=owner_b).json()["findings"] == []
    finally:
        app.dependency_overrides.clear()


def test_case_b_cannot_fetch_case_a_evidence_records(tmp_path: Path, monkeypatch) -> None:
    """The evidence-replay endpoint is keyed by source_id, not case_id in the URL --
    worth its own check that membership is still enforced via the source's own case_id."""
    client, case_a, _case_b, owner_a, owner_b = _two_cases_with_case_a_populated(tmp_path, monkeypatch)
    try:
        source_id = client.get(f"/v1/cases/{case_a}/sources", headers=owner_a).json()["sources"][0]["source_id"]
        allowed = client.get(f"/v1/evidence/{source_id}/records", headers=owner_a, params={"locator": "/0"})
        assert allowed.status_code == 200

        denied = client.get(f"/v1/evidence/{source_id}/records", headers=owner_b, params={"locator": "/0"})
        assert denied.status_code == 404
    finally:
        app.dependency_overrides.clear()
