from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine.adapters import SourceParseError, rows_for_source
from app.main import app
from app.models import FragmentReceipt, Snapshot
from workers import runner

TXID = "a" * 64


def _csv_payload() -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "timestamp",
            "src_ip",
            "dst_ip",
            "src_port",
            "dst_port",
            "txid",
            "input_addresses",
            "input_amounts",
            "output_addresses",
            "output_amounts",
            "fee",
            "script_type",
        ],
    )
    writer.writeheader()
    valid = {
        "timestamp": "2026-01-01T00:00:00Z",
        "src_ip": "198.51.100.1",
        "dst_ip": "203.0.113.2",
        "src_port": "8333",
        "dst_port": "8333",
        "txid": TXID,
        "input_addresses": '["in-1"]',
        "input_amounts": '["1.00000000"]',
        "output_addresses": '["out-1", "out-change"]',
        "output_amounts": '["0.40000000", "0.59999000"]',
        "fee": "0.00001000",
        "script_type": "p2wpkh",
    }
    writer.writerow(valid)
    writer.writerow({**valid, "txid": "b" * 64, "output_addresses": '["only-one"]', "output_amounts": '["0.1", "0.2"]'})
    writer.writerow(valid)
    return stream.getvalue().encode()


def _test_app(tmp_path: Path, monkeypatch):
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
    return TestClient(app), sessions, settings


def test_csv_ingestion_commits_receipted_parquet_and_quarantines_bad_rows(tmp_path: Path, monkeypatch) -> None:
    client, sessions, settings = _test_app(tmp_path, monkeypatch)
    headers = {"X-TraceX-Actor": "analyst"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Phase 2"}).json()["case_id"]
        payload = _csv_payload()
        response = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**headers, "Idempotency-Key": "phase-two-csv"},
            files={"file": ("transactions.csv", payload, "text/csv")},
        )
        assert response.status_code == 202, response.text
        job_id, source_id = response.json()["job_id"], response.json()["source_id"]
        assert runner.process_one("ingestion-worker")
        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        assert job["state"] == "completed"
        assert job["stage"] == "ingested"
        assert (job["rows_seen"], job["rows_accepted"], job["rows_quarantined"]) == (3, 1, 2)
        assert job["rows_seen"] == job["rows_accepted"] + job["rows_quarantined"]
        assert job["bytes_read"] == len(payload)
        assert job["snapshot_id"]

        evidence = client.get(f"/v1/evidence/{source_id}/records", headers=headers, params={"locator": "record:1"})
        assert evidence.status_code == 200
        assert evidence.json()["source_sha256"] == hashlib.sha256(payload).hexdigest()
        assert evidence.json()["record"]["txid"] == TXID

        with sessions() as session:
            receipts = session.query(FragmentReceipt).filter_by(job_id=job_id).all()
            snapshot = session.get(Snapshot, job["snapshot_id"])
            assert snapshot is not None and snapshot.state == "complete" and not snapshot.provisional
        counts = {receipt.record_type: receipt.record_count for receipt in receipts}
        assert counts == {"transactions": 1, "inputs": 1, "outputs": 2, "network_observations": 1, "quarantine": 2}
        for receipt in receipts:
            fragment = settings.evidence_root / receipt.storage_relative_path
            assert fragment.is_file()
            assert hashlib.sha256(fragment.read_bytes()).hexdigest() == receipt.sha256
        quarantine_receipt = next(receipt for receipt in receipts if receipt.record_type == "quarantine")
        quarantines = pq.read_table(settings.evidence_root / quarantine_receipt.storage_relative_path).to_pylist()
        assert any("equal length" in row["canonical_json"] for row in quarantines)
        assert any("duplicate transaction" in row["canonical_json"] for row in quarantines)
    finally:
        app.dependency_overrides.clear()


def test_json_and_xml_adapters_stream_stable_locators_and_reject_dtds(tmp_path: Path) -> None:
    json_path = tmp_path / "rows.json"
    json_path.write_text('[{"txid":"' + TXID + '"}]', encoding="utf-8")
    json_rows = list(rows_for_source(json_path, "json"))
    assert [(row.logical_record, row.locator_type, row.locator) for row in json_rows] == [(1, "json_pointer", "/0")]

    xml_path = tmp_path / "rows.xml"
    xml_path.write_text(
        f'<transactions><transaction txid="{TXID}" output_addresses="[&quot;out&quot;]" output_amounts="[&quot;1.0&quot;]" /></transactions>',
        encoding="utf-8",
    )
    xml_rows = list(rows_for_source(xml_path, "xml"))
    assert [(row.logical_record, row.locator_type, row.locator) for row in xml_rows] == [
        (1, "xml_element_path", "/transaction[1]")
    ]

    unsafe = tmp_path / "unsafe.xml"
    unsafe.write_text("<!DOCTYPE x [<!ENTITY boom 'nope'>]><transactions/>", encoding="utf-8")
    with pytest.raises(SourceParseError, match="forbidden"):
        list(rows_for_source(unsafe, "xml"))
