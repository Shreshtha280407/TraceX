from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine.motifs.deterministic import (
    detect_coinjoin_like_transactions,
    detect_peeling_chains,
    propagate_synthetic_review_seeds,
)
from app.main import app
from workers import runner


def _tx(number: int) -> str:
    return f"{number:064x}"


def _ref(number: int) -> list[dict]:
    return [{"evidence_id": "synthetic-fixture", "source_sha256": "a" * 64, "locator_type": "synthetic", "locator": f"record:{number}"}]


def _facts() -> tuple[dict[str, dict], list[dict], list[dict]]:
    transactions, inputs, outputs = {}, [], []
    amounts = [100_000, 90_000, 80_000, 70_000]
    addresses = ["seed", "hop1", "hop2", "hop3"]
    for index, amount in enumerate(amounts):
        txid = _tx(index + 1)
        transactions[txid] = {"txid": txid, "source_timestamp": f"2026-01-01T00:0{index}:00+00:00", "source_refs": _ref(index + 1)}
        outputs.append({"txid": txid, "vout": 0, "address": addresses[index], "amount_sats": amount, "source_refs": _ref(index + 1)})
        if index:
            inputs.append({"txid": txid, "vin": 0, "prev_txid": _tx(index), "prev_vout": 0, "source_refs": _ref(index + 1)})
    return transactions, inputs, outputs


def test_verified_peeling_chain_is_bounded_and_regular_sequence_is_not() -> None:
    transactions, inputs, outputs = _facts()
    findings = detect_peeling_chains(transactions=transactions, inputs=inputs, outputs=outputs)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["hop_count"] == 3
    assert finding["transaction_ids"] == [_tx(2), _tx(3), _tx(4)]
    assert 0 <= finding["score"] <= 1
    assert finding["evidence_refs"] and finding["graph_path"]["edge_ids"]
    assert not detect_peeling_chains(transactions=transactions, inputs=inputs, outputs=outputs, min_chain_length=4)
    # A sequence without an actually spent continuation cannot become a chain.
    assert not detect_peeling_chains(transactions=transactions, inputs=inputs[:2], outputs=outputs)


def test_coinjoin_like_shape_is_observable_only_and_thresholds_are_configurable() -> None:
    txid = _tx(20)
    transactions = {txid: {"txid": txid, "fee_sats": 1_000, "source_refs": _ref(20)}}
    inputs = [{"txid": txid, "vin": index, "source_refs": _ref(20)} for index in range(3)]
    outputs = [{"txid": txid, "vout": index, "amount_sats": amount, "source_refs": _ref(20)} for index, amount in enumerate([50_000, 50_001, 50_000, 2_000])]
    assert not detect_coinjoin_like_transactions(transactions=transactions, inputs=inputs, outputs=outputs)
    findings = detect_coinjoin_like_transactions(transactions=transactions, inputs=inputs, outputs=outputs, equality_tolerance_sats=1)
    assert findings[0]["equal_output_count"] == 3
    assert findings[0]["equal_output_value_sats"] == 50_000
    assert 0 <= findings[0]["score"] <= 1 and findings[0]["evidence_refs"]
    regular = outputs[:2] + [{"txid": txid, "vout": 2, "amount_sats": 40_000, "source_refs": _ref(20)}]
    assert not detect_coinjoin_like_transactions(transactions=transactions, inputs=inputs, outputs=regular)


def test_synthetic_seed_paths_are_verified_bounded_and_never_ip_derived() -> None:
    transactions, inputs, outputs = _facts()
    seeds = [{"id": "seed-1", "seed_entity_ref": "address:seed", "seed_reason": "synthetic fixture", "synthetic": True}]
    findings = propagate_synthetic_review_seeds(seeds=seeds, transactions=transactions, inputs=inputs, outputs=outputs, max_depth=2, decay=0.5)
    by_entity = {item["entity_ref"]: item for item in findings}
    assert by_entity["address:seed"]["direct_seed"] is True
    assert by_entity["address:hop1"]["score"] == 0.5
    assert by_entity["address:hop2"]["score"] == 0.25
    assert "address:hop3" not in by_entity
    assert "address:disconnected" not in by_entity  # disconnected means zero supported score
    assert all("endpoint:" not in node and "obs:" not in node for item in findings for node in item["graph_path"]["nodes"])
    assert propagate_synthetic_review_seeds(seeds=seeds, transactions=transactions, inputs=inputs, outputs=outputs, max_depth=0)[0]["score"] == 1.0


def test_phase41_api_response_shape_feature_export_and_synthetic_seed_isolation(tmp_path: Path, monkeypatch) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'phase41.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(f"sqlite:///{tmp_path / 'phase41.db'}", tmp_path / "evidence", 1024 * 1024, 3, 1)

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    client = TestClient(app)
    headers = {"X-TraceX-Actor": "phase41-reviewer"}
    try:
        fixture = (Path(__file__).parents[2] / "fixtures" / "phase4_1" / "motifs.ndjson").read_bytes()
        case = client.post("/v1/cases", headers=headers, json={"name": "phase41", "synthetic": True}).json()
        imported = client.post(
            f"/v1/cases/{case['case_id']}/imports", headers={**headers, "Idempotency-Key": "phase41-fixture"},
            files={"file": ("motifs.ndjson", fixture, "application/x-ndjson")},
        )
        assert imported.status_code == 202, imported.text
        assert runner.process_one("phase41-worker")
        job = client.get(f"/v1/jobs/{imported.json()['job_id']}", headers=headers).json()
        assert job["state"] == "completed", job.get("error_detail")
        snapshot_id = job["snapshot_id"]
        listed = client.get(f"/v1/cases/{case['case_id']}/findings", headers=headers).json()["findings"]
        assert any(item["finding_type"] == "peeling_chain_candidate" for item in listed), listed
        motif = next(item for item in listed if item["finding_type"] == "peeling_chain_candidate")
        assert {"finding_id", "finding_type", "entity_or_transaction_id", "snapshot_id", "score", "coverage", "uncertainty", "reason_codes", "explanation", "evidence_refs", "graph_path"} <= set(motif)
        assert 0 <= motif["score"] <= 1
        exported = client.get(f"/v1/cases/{case['case_id']}/features/export", headers=headers).json()
        assert exported["rows"]
        feature = exported["rows"][0]["features"]
        phase41_fields = {"peeling_chain_score", "peeling_chain_length", "peeling_chain_total_duration_sec", "peeling_chain_evidence_count", "coinjoin_like_score", "equal_output_count", "equal_output_value_sats", "coinjoin_like_evidence_count", "risk_propagation_score", "risk_seed_distance", "risk_path_evidence_count", "risk_seed_count"}
        assert phase41_fields <= set(feature)
        schema = json.loads((Path(__file__).parents[2] / "feature_schema.json").read_text(encoding="utf-8"))
        assert phase41_fields <= set(schema["properties"])
        seeded = client.post(
            f"/v1/cases/{case['case_id']}/synthetic-review-seeds", headers=headers,
            json={"seed_snapshot_id": snapshot_id, "seed_address_id": "bcrt1qseed", "seed_reason": "synthetic evaluation fixture"},
        )
        assert seeded.status_code == 201, seeded.text
        after_seed = client.get(f"/v1/cases/{case['case_id']}/findings", headers=headers).json()["findings"]
        assert any(item["finding_type"] == "synthetic_seed_proximity" for item in after_seed)
        non_synthetic = client.post("/v1/cases", headers=headers, json={"name": "real"}).json()
        rejected = client.post(
            f"/v1/cases/{non_synthetic['case_id']}/synthetic-review-seeds", headers=headers,
            json={"seed_snapshot_id": snapshot_id, "seed_address_id": "bcrt1qseed", "seed_reason": "not allowed"},
        )
        assert rejected.status_code == 422
    finally:
        app.dependency_overrides.clear()
