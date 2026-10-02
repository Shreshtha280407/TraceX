"""One review finding per pattern, and memory-adaptive chunking that never changes results."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import resources
from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine.feature_store import iter_feature_rows
from app.engine.findings import deterministic as findings_module
from app.engine.ingestion import pipeline as pipeline_module
from app.main import app
from app.models import FindingRecord
from workers import runner


def _tx(n: int) -> str:
    return f"{n:064x}"


def _peeling_chain(hops: int, base: int = 0, tag: str = "") -> list[dict]:
    """A fund transaction, then `hops` spends that each peel a small payment and
    carry the rest forward; the last continuation is never spent. `base` and
    `tag` give a second, unrelated chain its own txids and addresses."""
    rows = [
        {
            "txid": _tx(base + 1),
            "network": "bitcoin-regtest",
            "timestamp": "2026-01-01T00:00:00Z",
            "inputs": [],
            "outputs": [{"address": f"bcrt1q{tag}origin", "amount_sats": 1_000_000}],
            "fee_sats": 0,
        }
    ]
    amount = 1_000_000
    for hop in range(1, hops + 1):
        rows.append(
            {
                "txid": _tx(base + hop + 1),
                "network": "bitcoin-regtest",
                "timestamp": f"2026-01-01T00:{hop:02d}:00Z",
                "inputs": [{"prev_txid": _tx(base + hop), "prev_vout": 0, "address": f"bcrt1q{tag}hop{hop - 1}", "amount_sats": amount}],
                "outputs": [
                    {"address": f"bcrt1q{tag}hop{hop}", "amount_sats": amount - 60_000},
                    {"address": f"bcrt1q{tag}pay{hop}", "amount_sats": 50_000},
                ],
                "fee_sats": 10_000,
            }
        )
        amount -= 60_000
    return rows


def _collection_rows() -> list[dict]:
    """Three payments to one address within one minute: the rule is met in the
    15-minute, 1-hour and 24-hour windows alike."""
    return [
        {
            "txid": _tx(100 + index),
            "network": "bitcoin-regtest",
            "timestamp": f"2026-02-01T10:00:{index * 10:02d}Z",
            "inputs": [],
            "outputs": [{"address": "bcrt1qsink", "amount_sats": 10_000 + index}],
            "fee_sats": 0,
        }
        for index in range(3)
    ]


def _run(tmp_path: Path, monkeypatch, rows: list[dict], name: str):
    engine = make_engine(f"sqlite:///{tmp_path / f'{name}.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / f'{name}.db'}",
        evidence_root=tmp_path / f"{name}-evidence",
        max_upload_bytes=1024 * 1024,
        lease_seconds=30,
        event_heartbeat_seconds=1,
        ml_findings_enabled=False,
    )

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    client = TestClient(app)
    headers = {"X-TraceX-Actor": "grouping"}
    case_id = client.post("/v1/cases", headers=headers, json={"name": name, "synthetic": True}).json()["case_id"]
    response = client.post(
        f"/v1/cases/{case_id}/imports",
        headers={**headers, "Idempotency-Key": name},
        files={"file": (f"{name}.ndjson", "\n".join(json.dumps(row) for row in rows).encode(), "application/x-ndjson")},
    )
    assert response.status_code == 202, response.text
    assert runner.process_one(f"{name}-worker")
    job = client.get(f"/v1/jobs/{response.json()['job_id']}", headers=headers).json()
    assert job["state"] == "completed", job
    findings = client.get(f"/v1/cases/{case_id}/findings", headers=headers, params={"limit": 200}).json()
    with sessions() as session:
        features = sorted(
            (row["entity_ref"], row["window_start"], row["window_end"], json.dumps(row["features"], sort_keys=True))
            for row in iter_feature_rows(session, settings.evidence_root, case_id=case_id)
        )
        # Detector evidence refs carry the per-import source id; normalise it so
        # two imports of the same bytes compare equal.
        source_id = response.json()["source_id"]
        stored = sorted(
            (
                row.entity_ref,
                row.window_start.isoformat(),
                row.rule_id,
                row.raw_score,
                row.rank,
                json.dumps(row.feature_vector, sort_keys=True).replace(source_id, "<source>"),
                json.dumps(row.source_refs, sort_keys=True).replace(source_id, "<source>"),
            )
            for row in session.query(FindingRecord)
        )
    app.dependency_overrides.clear()
    return findings, features, stored


def test_a_peeling_chain_and_its_tails_are_one_finding(tmp_path: Path, monkeypatch) -> None:
    findings, _, _ = _run(tmp_path, monkeypatch, _peeling_chain(5), "chain")
    peeling = [item for item in findings["findings"] if item["rule_id"] == "peeling_chain_candidate"]
    # The detector still sees the 5-, 4- and 3-hop walks; they are one pattern.
    assert len(peeling) == 1, peeling
    (pattern,) = peeling
    assert pattern["hop_count"] == 5
    assert pattern["entity_ref"] == "address:bcrt1qorigin"
    assert pattern["pattern"]["chain_count"] == 3
    assert pattern["pattern"]["transaction_count"] == 5
    assert set(pattern["pattern"]["transaction_ids"]) == {f"tx:{_tx(n)}" for n in range(2, 7)}
    assert "merged_overlapping_chains" in pattern["reason_codes"]
    assert "address:bcrt1qhop4" in pattern["pattern"]["addresses"]


def test_nested_windows_of_one_burst_are_one_finding(tmp_path: Path, monkeypatch) -> None:
    findings, features, _ = _run(tmp_path, monkeypatch, _collection_rows(), "burst")
    collection = [item for item in findings["findings"] if item["rule_id"] == "concentrated_collection"]
    assert len(collection) == 1, collection
    windows = collection[0]["matched_windows"]
    assert [window["window_seconds"] for window in windows] == [900, 3600, 86400]
    assert "reviewed once" in collection[0]["explanations"][-1]
    # Feature rows (the model/export input) are not grouped: one per window.
    assert sum(1 for row in features if row[0] == "address:bcrt1qsink") == 3


def test_small_memory_plan_gives_identical_results(tmp_path: Path, monkeypatch) -> None:
    rows = _peeling_chain(6) + _collection_rows()
    _, big_features, big_findings = _run(tmp_path, monkeypatch, rows, "roomy")

    tiny = replace(resources.current_plan(), insert_chunk_rows=2, max_ingestion_batch_records=3)
    monkeypatch.setattr(findings_module, "current_plan", lambda: tiny)
    monkeypatch.setattr(pipeline_module, "current_plan", lambda: tiny)
    _, small_features, small_findings = _run(tmp_path, monkeypatch, rows, "tight")

    assert small_features == big_features
    assert small_findings == big_findings


def test_resource_plan_is_bounded_and_never_raises_the_configured_batch(monkeypatch) -> None:
    plan = resources.current_plan()
    assert plan.cpu_count >= 1 and plan.memory_budget_bytes >= 64 * resources.MB
    assert 2_000 <= plan.insert_chunk_rows <= 25_000
    assert plan.ingestion_batch_records(100) == 100
    assert plan.ingestion_batch_records(10**9) == plan.max_ingestion_batch_records

    monkeypatch.setenv("TRACEX_MEMORY_BUDGET_MB", "256")
    small = resources.current_plan()
    assert small.memory_budget_bytes == 256 * resources.MB
    assert small.insert_chunk_rows == 2_000
    assert small.max_ingestion_batch_records == 2_184
    assert small.duckdb_memory_limit_mb == 256

    monkeypatch.setenv("TRACEX_MEMORY_BUDGET_MB", "not-a-number")
    assert resources.current_plan().memory_budget_bytes >= 256 * resources.MB


def test_healthz_reports_the_machine_profile(tmp_path: Path, monkeypatch) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_session():
        with sessions() as session:
            yield session

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'health.db'}",
        evidence_root=tmp_path / "evidence",
        max_upload_bytes=1024,
        lease_seconds=3,
        event_heartbeat_seconds=1,
    )
    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    try:
        body = TestClient(app).get("/v1/healthz").json()
    finally:
        app.dependency_overrides.clear()
    assert body["status"] == "ok"
    assert body["resources"]["cpu_count"] >= 1
    assert body["resources"]["insert_chunk_rows"] >= 2_000


def _mixed_rows() -> list[dict]:
    """Every detector family at once: a peeling chain, a collection burst, an
    equal-output (coinjoin-like) transaction, and a rapid re-spend."""
    rows = _peeling_chain(6) + _collection_rows()
    funding = [
        {
            "txid": _tx(300 + index),
            "network": "bitcoin-regtest",
            "timestamp": f"2026-03-01T09:0{index}:00Z",
            "inputs": [],
            "outputs": [{"address": f"bcrt1qmix{index}", "amount_sats": 500_000}],
            "fee_sats": 0,
        }
        for index in range(3)
    ]
    mixer = {
        "txid": _tx(400),
        "network": "bitcoin-regtest",
        "timestamp": "2026-03-01T09:30:00Z",
        "inputs": [
            {"prev_txid": _tx(300 + index), "prev_vout": 0, "address": f"bcrt1qmix{index}", "amount_sats": 500_000}
            for index in range(3)
        ],
        "outputs": [{"address": f"bcrt1qeq{index}", "amount_sats": 400_000} for index in range(3)],
        "fee_sats": 300_000,
    }
    sweep = {
        "txid": _tx(500),
        "network": "bitcoin-regtest",
        "timestamp": "2026-02-01T10:20:00Z",
        "inputs": [
            {"prev_txid": _tx(100 + index), "prev_vout": 0, "address": "bcrt1qsink", "amount_sats": 10_000 + index}
            for index in range(3)
        ],
        "outputs": [{"address": "bcrt1qonward", "amount_sats": 29_000}],
        "fee_sats": 3,
    }
    return rows + funding + [mixer, sweep]


def test_bounded_and_in_memory_execution_produce_identical_results(tmp_path: Path, monkeypatch) -> None:
    import duckdb

    from app.models import GraphSnapshot

    def graph_content(name: str) -> list:
        engine = make_engine(f"sqlite:///{tmp_path / f'{name}.db'}")
        with sessionmaker(bind=engine)() as session:
            graph = session.query(GraphSnapshot).one()
            path = tmp_path / f"{name}-evidence" / graph.storage_relative_path
            coverage, counts = graph.coverage, (graph.node_count, graph.edge_count)
        connection = duckdb.connect(str(path), read_only=True)
        try:
            return [
                coverage, counts,
                connection.execute("SELECT * FROM nodes ORDER BY node_id").fetchall(),
                connection.execute("SELECT * FROM edges ORDER BY edge_id").fetchall(),
            ]
        finally:
            connection.close()

    rows = _mixed_rows()
    monkeypatch.setenv("TRACEX_EXECUTION_MODE", "memory")
    memory_findings, memory_features, memory_rows = _run(tmp_path, monkeypatch, rows, "memory")
    monkeypatch.setenv("TRACEX_EXECUTION_MODE", "bounded")
    _, bounded_features, bounded_rows = _run(tmp_path, monkeypatch, rows, "bounded")

    rules = {item["rule_id"] for item in memory_findings["findings"]}
    assert {"peeling_chain_candidate", "concentrated_collection", "coinjoin_like_structure", "rapid_redistribution"} <= rules
    assert bounded_features == memory_features
    assert bounded_rows == memory_rows
    # No network observations in this data, so no node id embeds the
    # per-import source id: the two graphs must match exactly.
    assert graph_content("bounded") == graph_content("memory")


def test_worker_processes_give_identical_results(tmp_path: Path, monkeypatch) -> None:
    """Bounded execution split over worker processes -- peeling chunks, address
    partitions and detector slices, each as small as possible -- stores exactly
    what the in-memory path stores."""
    from app.engine import bounded

    rows = _mixed_rows() + _peeling_chain(5, base=1_000, tag="b")
    monkeypatch.setenv("TRACEX_EXECUTION_MODE", "memory")
    memory_findings, memory_features, memory_rows = _run(tmp_path, monkeypatch, rows, "memory")
    monkeypatch.setenv("TRACEX_EXECUTION_MODE", "bounded")
    monkeypatch.setenv("TRACEX_WORKERS", "2")
    monkeypatch.setattr(bounded, "_chain_chunk", lambda plan: 1)
    monkeypatch.setattr(bounded, "_MIN_PARALLEL_CHUNK", 1)
    _, parallel_features, parallel_rows = _run(tmp_path, monkeypatch, rows, "parallel")

    assert sum(1 for item in memory_findings["findings"] if item["rule_id"] == "peeling_chain_candidate") == 2
    assert parallel_features == memory_features
    assert parallel_rows == memory_rows
