"""Run the deterministic Phase 5A input-contract smoke path with no ML components."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app
from workers import runner

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "fixtures" / "ml_smoke_10k"
PHASE41_FIELDS = {
    "peeling_chain_score", "peeling_chain_length", "peeling_chain_total_duration_sec", "peeling_chain_evidence_count",
    "coinjoin_like_score", "equal_output_count", "equal_output_value_sats", "coinjoin_like_evidence_count",
    "risk_propagation_score", "risk_seed_distance", "risk_path_evidence_count", "risk_seed_count",
}


def fixture_generator():
    spec = importlib.util.spec_from_file_location("phase5a_fixture_generator", FIXTURE / "generate.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Phase 5A fixture generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_model_input_contract(features: dict, schema: dict) -> None:
    if not PHASE41_FIELDS.issubset(schema["properties"]):
        raise RuntimeError("feature_schema.json does not include every Phase 4.1 field")
    prohibited = {"address", "wallet", "raw_ip", "src_ip", "dst_ip", "txid", "scenario", "seed", "seed_identity"}
    if prohibited.intersection(features):
        raise RuntimeError("a prohibited raw identifier appears in the feature vector")
    handoff = (REPO / "docs" / "phase5a_handoff.md").read_text(encoding="utf-8").lower()
    if "must **not** be model input features" not in handoff:
        raise RuntimeError("Phase 5A handoff lacks the model-input exclusion rule")


def main() -> None:
    started = time.perf_counter()
    generator = fixture_generator()
    manifest = generator.generate(FIXTURE)
    generator.validate(FIXTURE)
    truth = json.loads((FIXTURE / "evaluation_truth.json").read_text(encoding="utf-8"))
    payload = (FIXTURE / "ingestion_rows.ndjson").read_bytes()
    with tempfile.TemporaryDirectory(prefix="tracex-phase5a-smoke-") as temporary:
        root = Path(temporary)
        engine = make_engine(f"sqlite:///{root / 'control.db'}")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        settings = Settings(f"sqlite:///{root / 'control.db'}", root / "evidence", len(payload) + 1, 60, 1, ingestion_batch_records=512)

        def override_session():
            with sessions() as session:
                yield session

        old_route_settings, old_worker_settings, old_sessions = routes.settings, runner.settings, runner.SessionLocal
        app.dependency_overrides[get_session] = override_session
        routes.settings, runner.settings, runner.SessionLocal = settings, settings, sessions
        client = TestClient(app)
        headers = {"X-TraceX-Actor": "phase5a-smoke"}
        try:
            case_response = client.post("/v1/cases", headers=headers, json={"name": "Phase 5A 10K smoke", "synthetic": True})
            case_response.raise_for_status()
            case_id = case_response.json()["case_id"]
            upload = client.post(
                f"/v1/cases/{case_id}/imports", headers={**headers, "Idempotency-Key": "phase5a-smoke-10k"},
                files={"file": ("ingestion_rows.ndjson", payload, "application/x-ndjson")},
            )
            upload.raise_for_status()
            job_id = upload.json()["job_id"]
            if not runner.process_one("phase5a-smoke-worker"):
                raise RuntimeError("worker did not process smoke fixture")
            job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
            if job["state"] != "completed" or job["rows_accepted"] != 10_000 or job["rows_quarantined"] != manifest["counts"]["duplicate_candidates"]:
                raise RuntimeError(f"ingestion did not preserve 10K canonical rows: {job}")
            findings = client.get(f"/v1/cases/{case_id}/findings", headers=headers).json()["findings"]
            types = {item["finding_type"] for item in findings}
            if not {"peeling_chain_candidate", "coinjoin_like_structure"}.issubset(types):
                raise RuntimeError(f"missing expected deterministic findings: {types}")
            seed = client.post(
                f"/v1/cases/{case_id}/synthetic-review-seeds", headers=headers,
                json={"seed_snapshot_id": job["snapshot_id"], "seed_address_id": truth["synthetic_review_seed_address"], "seed_reason": "synthetic Phase 5A smoke evaluation"},
            )
            seed.raise_for_status()
            post_seed = client.get(f"/v1/cases/{case_id}/findings", headers=headers).json()["findings"]
            propagation = [item for item in post_seed if item["finding_type"] == "synthetic_seed_proximity"]
            two_hop = next((item for item in propagation if item["entity_or_transaction_id"] == f"address:{truth['expected_two_hop_target_address']}"), None)
            if two_hop is None or two_hop["score"] <= 0:
                raise RuntimeError("expected two-hop propagated target was not found")
            if any(item["entity_or_transaction_id"] == f"address:{truth['disconnected_address']}" for item in propagation):
                raise RuntimeError("disconnected address received unsupported propagated context")
            if any(any(node.startswith(("endpoint:", "obs:")) for node in (item.get("graph_path") or {}).get("nodes", [])) for item in propagation):
                raise RuntimeError("IP/endpoint observation leaked into a propagation graph path")
            exported = client.get(f"/v1/cases/{case_id}/features/export", headers=headers).json()
            if not exported["rows"] or exported["feature_schema_version"] != "phase4.1-feature-v1":
                raise RuntimeError("feature export is empty or has the wrong schema version")
            schema = json.loads((REPO / "feature_schema.json").read_text(encoding="utf-8"))
            for row in exported["rows"]:
                if not PHASE41_FIELDS.issubset(row["features"]):
                    raise RuntimeError("feature row lacks a required Phase 4.1 field")
                _validate_model_input_contract(row["features"], schema)
            target_rows = [row for row in exported["rows"] if row["entity_ref"] == f"address:{truth['expected_two_hop_target_address']}"]
            if not target_rows or max(row["features"]["risk_seed_distance"] or 0 for row in target_rows) != 2:
                raise RuntimeError("two-hop target feature row does not retain the verified seed distance")
            with sessions() as session:
                from app.models import GraphSnapshot

                graph = session.query(GraphSnapshot).filter_by(case_id=case_id).one_or_none()
                if graph is None:
                    raise RuntimeError("graph snapshot is missing")
                graph_counts = (graph.node_count, graph.edge_count)
        finally:
            app.dependency_overrides.clear()
            routes.settings, runner.settings, runner.SessionLocal = old_route_settings, old_worker_settings, old_sessions
    elapsed = time.perf_counter() - started
    print(
        "Phase 5A smoke passed: "
        f"canonical_rows={manifest['counts']['transactions']} ingestion_rows={manifest['counts']['ingestion_rows']} "
        f"graph_nodes={graph_counts[0]} graph_edges={graph_counts[1]} findings={len(post_seed)} feature_rows={len(exported['rows'])} elapsed_sec={elapsed:.2f}"
    )


if __name__ == "__main__":
    main()
