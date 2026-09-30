"""Source-centred fund flow, per-format record counting, and additive schema upgrade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, ensure_schema, get_session, make_engine
from app.engine.adapters import count_records, rows_for_source
from app.main import app
from workers import runner

FUND_TXID = "a" * 64
SPLIT_TXID = "b" * 64
ONWARD_TXID = "c" * 64

# FUND pays `funding`; SPLIT spends it into `pay` (spent again by ONWARD) and
# `change` (never moves again); ONWARD pays `final`.
ROWS = [
    {
        "txid": FUND_TXID,
        "network": "bitcoin-regtest",
        "inputs": [],
        "outputs": [{"address": "bcrt1qfunding", "amount_sats": 1_000_000}],
        "fee_sats": 0,
        "timestamp": "2026-01-01T00:00:00Z",
    },
    {
        "txid": SPLIT_TXID,
        "network": "bitcoin-regtest",
        "inputs": [{"prev_txid": FUND_TXID, "prev_vout": 0, "address": "bcrt1qfunding", "amount_sats": 1_000_000}],
        "outputs": [
            {"address": "bcrt1qpay", "amount_sats": 600_000},
            {"address": "bcrt1qchange", "amount_sats": 399_000},
        ],
        "fee_sats": 1_000,
        "timestamp": "2026-01-01T00:10:00Z",
    },
    {
        "txid": ONWARD_TXID,
        "network": "bitcoin-regtest",
        "inputs": [{"prev_txid": SPLIT_TXID, "prev_vout": 0, "address": "bcrt1qpay", "amount_sats": 600_000}],
        "outputs": [{"address": "bcrt1qfinal", "amount_sats": 590_000}],
        "fee_sats": 10_000,
        "timestamp": "2026-01-01T00:20:00Z",
    },
]


@pytest.fixture()
def flow_case(tmp_path: Path, monkeypatch):
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
    client = TestClient(app)
    headers = {"X-TraceX-Actor": "flow-analyst"}
    case_id = client.post("/v1/cases", headers=headers, json={"name": "flow"}).json()["case_id"]
    # A JSON array on purpose: it used to report no total at all.
    response = client.post(
        f"/v1/cases/{case_id}/imports",
        headers={**headers, "Idempotency-Key": "flow"},
        files={"file": ("flow.json", json.dumps(ROWS).encode(), "application/json")},
    )
    assert response.status_code == 202, response.text
    assert runner.process_one("flow-worker")
    job = client.get(f"/v1/jobs/{response.json()['job_id']}", headers=headers).json()
    try:
        yield client, headers, case_id, job
    finally:
        app.dependency_overrides.clear()


def _flow(client, headers, case_id, node):
    return client.get(f"/v1/cases/{case_id}/graph/flow", headers=headers, params={"node": node})


def test_json_import_reports_its_real_total(flow_case) -> None:
    _, _, _, job = flow_case
    assert job["state"] == "completed", job
    assert job["total_records"] == len(ROWS) == job["rows_seen"]
    assert job["progress"]["percent"] == 100.0
    assert "3 of 3 records parsed" in job["progress"]["basis"]


def test_transaction_source_shows_every_direct_input_and_output(flow_case) -> None:
    client, headers, case_id, _ = flow_case
    response = _flow(client, headers, case_id, SPLIT_TXID)  # bare txid resolves to tx:
    assert response.status_code == 200, response.text
    flow = response.json()
    assert flow["center"]["id"] == f"tx:{SPLIT_TXID}" and flow["center"]["selectable"] is True
    assert [(item["id"], item["amount_sats"]) for item in flow["inputs"]] == [("address:bcrt1qfunding", 1_000_000)]
    outputs = {item["id"]: item for item in flow["outputs"]}
    assert set(outputs) == {"address:bcrt1qpay", "address:bcrt1qchange"}
    assert flow["totals"] == {
        "input_count": 1,
        "output_count": 2,
        "input_sats": 1_000_000,
        "output_sats": 999_000,
        "dead_end_inputs": 0,
        "dead_end_outputs": 1,
    }
    # `pay` is spent onward; `change` never moves again -> a dead end with a reason.
    assert outputs["address:bcrt1qpay"]["selectable"] is True
    assert outputs["address:bcrt1qpay"]["onward_count"] == 1
    assert outputs["address:bcrt1qpay"]["via"] == [f"tx:{ONWARD_TXID}"]
    assert outputs["address:bcrt1qchange"]["selectable"] is False
    assert outputs["address:bcrt1qchange"]["onward_count"] == 0
    assert "No other transaction" in outputs["address:bcrt1qchange"]["reason"]
    # Largest amount first.
    assert [item["id"] for item in flow["outputs"]] == ["address:bcrt1qpay", "address:bcrt1qchange"]


def test_address_source_lists_paying_and_spending_transactions(flow_case) -> None:
    client, headers, case_id, _ = flow_case
    flow = _flow(client, headers, case_id, "bcrt1qpay").json()  # bare address resolves to address:
    assert flow["center"]["id"] == "address:bcrt1qpay"
    assert flow["center"]["transaction_count"] == 2
    (paid_by,) = flow["inputs"]
    (spent_in,) = flow["outputs"]
    assert (paid_by["id"], paid_by["amount_sats"], paid_by["type"]) == (f"tx:{SPLIT_TXID}", 600_000, "transaction")
    assert (spent_in["id"], spent_in["amount_sats"]) == (f"tx:{ONWARD_TXID}", 600_000)
    # SPLIT also touches `funding` and `change`; ONWARD also pays `final`.
    assert paid_by["onward_count"] == 2 and paid_by["selectable"] is True
    assert spent_in["onward_count"] == 1 and spent_in["selectable"] is True
    assert paid_by["timestamp"] and spent_in["timestamp"]


def test_transaction_whose_only_leg_is_the_source_is_a_dead_end(flow_case) -> None:
    client, headers, case_id, _ = flow_case
    flow = _flow(client, headers, case_id, "address:bcrt1qfunding").json()
    (funded_by,) = flow["inputs"]
    assert funded_by["id"] == f"tx:{FUND_TXID}"
    # FUND has no resolved inputs and pays only this address: nothing further.
    assert funded_by["selectable"] is False and funded_by["onward_count"] == 0
    assert funded_by["reason"]


def test_output_source_resolves_to_its_address_and_unknown_node_is_404(flow_case) -> None:
    client, headers, case_id, _ = flow_case
    flow = _flow(client, headers, case_id, f"out:{SPLIT_TXID}:1").json()
    assert flow["center"]["id"] == "address:bcrt1qchange"
    assert flow["center"]["resolved_from"] == f"out:{SPLIT_TXID}:1"
    assert flow["outputs"] == [] and len(flow["inputs"]) == 1
    missing = _flow(client, headers, case_id, "address:not-in-this-case")
    assert missing.status_code == 404
    endpoint = _flow(client, headers, case_id, "endpoint:198.51.100.1:8333")
    assert endpoint.status_code in {404, 422}


@pytest.mark.parametrize(
    ("source_format", "payload"),
    [
        # Quoted newline inside a field, a blank line, and an escaped quote.
        ("csv", 'a,b\r\n1,"multi\nline"\r\n\r\n2,"say ""hi"""\r\n3,4'),
        ("ndjson", '{"a":1}\n\n   \n{"a":2}\n{"a":3}'),
        ("json", '[{"a":[1,{"b":"]"}]},{"a":2},\n{"a":3}]'),
        (
            "xml",
            (
                '<?xml version="1.0"?><records><record><txid>1</txid></record>'
                '<record kind="x"><txid>2</txid></record><record><txid>3</txid></record></records>'
            ),
        ),
    ],
)
def test_count_records_matches_the_parser_for_every_format(tmp_path: Path, source_format: str, payload: str) -> None:
    path = tmp_path / f"source.{source_format}"
    path.write_text(payload, encoding="utf-8")
    parsed = sum(1 for _ in rows_for_source(path, source_format))
    assert parsed == 3
    assert count_records(path, source_format) == parsed


def test_ensure_schema_adds_a_column_missing_from_an_older_database(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE import_jobs (id VARCHAR(36) PRIMARY KEY)"))
    assert "total_records" not in {column["name"] for column in inspect(engine).get_columns("import_jobs")}
    assert ensure_schema(engine) == ["import_jobs.total_records"]
    assert "total_records" in {column["name"] for column in inspect(engine).get_columns("import_jobs")}
    assert ensure_schema(engine) == []
