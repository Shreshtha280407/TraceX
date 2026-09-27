from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app
from app.models import GraphSnapshot
from workers import runner

OLD_TXID = "a" * 64
NEW_TXID = "b" * 64


def _client(tmp_path: Path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'control.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        evidence_root=tmp_path / "evidence",
        max_upload_bytes=1024 * 1024,
        lease_seconds=3,
        event_heartbeat_seconds=1,
    )

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    return TestClient(app), sessions


def test_utxo_graph_is_output_mediated_and_query_is_bounded(tmp_path: Path, monkeypatch) -> None:
    client, sessions = _client(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "graph-analyst"}
    rows = [
        {
            "txid": OLD_TXID,
            "network": "bitcoin-regtest",
            "inputs": [],
            "outputs": [{"address": "bcrt1qfunding", "amount_sats": 1_000_000}],
            "fee_sats": 0,
        },
        {
            "txid": NEW_TXID,
            "network": "bitcoin-regtest",
            "inputs": [{"prev_txid": OLD_TXID, "prev_vout": 0, "address": "bcrt1qfunding", "amount_sats": 1_000_000}],
            "outputs": [
                {"address": "bcrt1qpay", "amount_sats": 600_000},
                {"address": "bcrt1qchange", "amount_sats": 399_000},
            ],
            "fee_sats": 1_000,
            "timestamp": "2026-01-01T00:00:00Z",
            "src_ip": "198.51.100.1",
            "dst_ip": "203.0.113.1",
            "src_port": 8333,
            "dst_port": 8333,
        },
    ]
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "UTXO graph"}).json()["case_id"]
        response = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**headers, "Idempotency-Key": "utxo-graph"},
            files={
                "file": ("utxo.ndjson", "\n".join(json.dumps(row) for row in rows).encode(), "application/x-ndjson")
            },
        )
        assert response.status_code == 202, response.text
        assert runner.process_one("graph-worker")
        graph_response = client.get(
            f"/v1/cases/{case_id}/graph", headers=headers, params={"seed": f"out:{OLD_TXID}:0", "depth": 1}
        )
        assert graph_response.status_code == 200, graph_response.text
        graph = graph_response.json()
        assert graph["coverage"]["resolved_spend_inputs"] == 1
        assert graph["coverage"]["missing_outpoint_inputs"] == 0
        assert graph["coverage"]["value_violations"] == 0
        edges = {(edge["from"], edge["to"], edge["type"]) for edge in graph["edges"]}
        assert (f"tx:{OLD_TXID}", f"out:{OLD_TXID}:0", "CREATES_OUTPUT") in edges
        assert (f"out:{OLD_TXID}:0", f"tx:{NEW_TXID}", "SPENT_BY") in edges
        assert (f"tx:{OLD_TXID}", f"tx:{NEW_TXID}", "SPENT_BY") not in edges
        assert any(
            edge["type"] == "OBSERVED_TX"
            for edge in client.get(
                f"/v1/cases/{case_id}/graph", headers=headers, params={"seed": NEW_TXID, "depth": 1}
            ).json()["edges"]
        )
        assert (
            client.get(
                f"/v1/cases/{case_id}/graph", headers=headers, params={"seed": OLD_TXID, "edge_limit": 3001}
            ).status_code
            == 422
        )
        with sessions() as session:
            stored = session.query(GraphSnapshot).filter_by(case_id=case_id).one()
            assert stored.node_count >= 9 and stored.edge_count >= 8
            assert stored.coverage["spend_lineage_complete"] is True
    finally:
        app.dependency_overrides.clear()
