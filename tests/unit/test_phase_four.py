from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine.findings.deterministic import _history_features
from app.main import app
from workers import runner


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
    return TestClient(app)


def _tx(number: int) -> str:
    return f"{number:064x}"


def test_history_features_surge_and_first_observed_activity() -> None:
    hour = 3600
    t1 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    t3 = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    output_events = {
        ("addr-a", hour, t1): [{"txid": _tx(1), "amount_sats": 1000}],
        ("addr-a", hour, t2): [{"txid": _tx(2), "amount_sats": 1000}, {"txid": _tx(3), "amount_sats": 9000}],
        ("addr-a", hour, t3): [{"txid": _tx(4), "amount_sats": 500}],
        ("addr-b", hour, t1): [{"txid": _tx(5), "amount_sats": 5000}],
    }
    history = _history_features(output_events)

    first = history[("addr-a", hour, t1)]
    assert first["first_observed_activity"] is True
    assert first["prior_window_gap_seconds"] is None
    assert first["activity_surge_ratio"] is None and first["value_surge_ratio"] is None

    second = history[("addr-a", hour, t2)]
    assert second["first_observed_activity"] is False
    assert second["prior_window_gap_seconds"] == 3600
    assert second["baseline_in_event_count_mean"] == 1
    assert second["activity_surge_ratio"] == 2  # two distinct txids this window vs baseline of 1
    assert second["baseline_value_sats_mean"] == 1000
    assert second["value_surge_ratio"] == 10  # 10,000 sats this window vs baseline of 1,000

    third = history[("addr-a", hour, t3)]
    assert third["baseline_in_event_count_mean"] == 1.5  # running mean of [1, 2]
    assert third["activity_surge_ratio"] == 1 / 1.5

    # addr-b's single window is independent of addr-a's history.
    assert history[("addr-b", hour, t1)]["first_observed_activity"] is True


def test_deterministic_findings_are_replayable_and_reviews_are_versioned(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    owner = {"X-TraceX-Actor": "case-lead"}
    funding = [_tx(1), _tx(2), _tx(3)]
    collections = [_tx(11), _tx(12), _tx(13)]
    sink = "bcrt1qsink"
    rows = []
    for index, txid in enumerate(funding):
        rows.append(
            {
                "txid": txid,
                "network": "bitcoin-regtest",
                "timestamp": f"2026-01-01T00:00:{index * 10:02d}Z",
                "inputs": [],
                "outputs": [{"address": f"bcrt1qfund{index}", "amount_sats": 100_000}],
                "fee_sats": 0,
            }
        )
    for index, txid in enumerate(collections):
        rows.append(
            {
                "txid": txid,
                "network": "bitcoin-regtest",
                "timestamp": f"2026-01-01T00:00:{30 + index * 10:02d}Z",
                "inputs": [
                    {
                        "prev_txid": funding[index],
                        "prev_vout": 0,
                        "address": f"bcrt1qfund{index}",
                        "amount_sats": 100_000,
                    }
                ],
                "outputs": [{"address": sink, "amount_sats": 99_000}],
                "fee_sats": 1_000,
                "analyst_context": "authorised synthetic exchange-treasury sweep control",
            }
        )
    rows.append(
        {
            "txid": _tx(20),
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:01:00Z",
            "inputs": [{"prev_txid": collections[0], "prev_vout": 0, "address": sink, "amount_sats": 99_000}],
            "outputs": [{"address": "bcrt1qdestination", "amount_sats": 98_000}],
            "fee_sats": 1_000,
        }
    )
    try:
        case_id = client.post(
            "/v1/cases", headers=owner, json={"name": "Deterministic findings", "synthetic": True}
        ).json()["case_id"]
        client.post(f"/v1/cases/{case_id}/members", headers=owner, json={"actor": "reviewer", "role": "reviewer"})
        imported = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**owner, "Idempotency-Key": "phase-four"},
            files={
                "file": ("motifs.ndjson", "\n".join(json.dumps(row) for row in rows).encode(), "application/x-ndjson")
            },
        )
        assert imported.status_code == 202, imported.text
        assert runner.process_one("finding-worker")
        job = client.get(f"/v1/jobs/{imported.json()['job_id']}", headers=owner)
        assert job.json()["state"] == "completed", job.text
        listed = client.get(f"/v1/cases/{case_id}/findings", headers=owner)
        assert listed.status_code == 200
        findings = listed.json()["findings"]
        assert listed.json()["ml_enabled"] is False
        assert {finding["rule_id"] for finding in findings} >= {"concentrated_collection", "rapid_redistribution"}
        collection = next(finding for finding in findings if finding["rule_id"] == "concentrated_collection")
        assert collection["entity_ref"] == f"address:{sink}"
        assert collection["coverage"]["complete"] is True
        assert collection["coverage"]["window_boundary_incomplete"] is True
        evidence = client.get(f"/v1/findings/{collection['finding_id']}/evidence", headers=owner)
        assert evidence.status_code == 200
        assert evidence.json()["replay_contract"]["ml_enabled"] is False
        assert evidence.json()["source_refs"]
        assert evidence.json()["opposing_evidence"]
        assert evidence.json()["feature_vector"]["inter_event_gaps_seconds"]["count"] == 2
        assert evidence.json()["feature_vector"]["bounded_component_change"]["scope"] == "one_hop_address_window"
        source_ref = evidence.json()["source_refs"][0]
        raw_source = client.get(
            f"/v1/evidence/{source_ref['evidence_id']}/records",
            headers=owner,
            params={"locator": source_ref["locator"]},
        )
        assert raw_source.status_code == 200
        assert raw_source.json()["record"]["analyst_context"] == "authorised synthetic exchange-treasury sweep control"
        reviewed = client.post(
            f"/v1/findings/{collection['finding_id']}/reviews",
            headers={"X-TraceX-Actor": "reviewer"},
            json={
                "expected_finding_version": 1,
                "disposition": "dismissed",
                "reason": "Exact source record identifies this synthetic lookalike as an authorised treasury-sweep control.",
                "counterevidence_refs": [source_ref],
            },
        )
        assert reviewed.status_code == 201, reviewed.text
        assert reviewed.json()["finding"]["finding_version"] == 2
        assert reviewed.json()["finding"]["status"] == "dismissed"
        reviewed_evidence = client.get(f"/v1/findings/{collection['finding_id']}/evidence", headers=owner).json()
        assert reviewed_evidence["review_history"][0]["counterevidence_refs"] == [source_ref]
        assert reviewed_evidence["audit_history"][0]["action"] == "finding.reviewed"
        exported = client.get(f"/v1/cases/{case_id}/findings/export", headers=owner)
        assert exported.status_code == 200
        assert exported.json()["ml_enabled"] is False
        assert any(item["finding"]["finding_id"] == collection["finding_id"] for item in exported.json()["findings"])
        conflict = client.post(
            f"/v1/findings/{collection['finding_id']}/reviews",
            headers={"X-TraceX-Actor": "reviewer"},
            json={"expected_finding_version": 1, "disposition": "dismissed", "reason": "stale review"},
        )
        assert conflict.status_code == 409
    finally:
        app.dependency_overrides.clear()
