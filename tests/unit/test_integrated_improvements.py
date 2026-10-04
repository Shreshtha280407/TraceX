"""Small fixtures only. No existing datasets, holdouts, vaults or Docker are touched."""
import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app.engine.evidence import EMPTY_COUNTER, structured_evidence
from app.ml import candidate
from app.models import Case, EvidenceSource, FindingRecord, ImportJob
from scripts import candidate_lifecycle, macbook_benchmark
from scripts.export_review_package import sanitize


def fitted(tmp_path):
    rng = np.random.default_rng(18)
    matrix = rng.normal(size=(64, len(candidate.COLUMNS))).astype(np.float32)
    calibration = rng.normal(size=(64, len(candidate.COLUMNS))).astype(np.float32)
    labels = {task: matrix[:, i] > 0 for i, task in enumerate(candidate.TASKS)}
    clabels = {task: calibration[:, i] > 0 for i, task in enumerate(candidate.TASKS)}
    models = candidate.train(matrix, labels, calibration, clabels)
    manifest = candidate.save(tmp_path / "artifact", models, name="hist", provenance={"tiny_fixture_only": True})
    return matrix, models, manifest, tmp_path / "artifact"


def test_candidate_frozen_serialization_batch_parity_integrity_and_eligibility(tmp_path):
    matrix, models, manifest, path = fitted(tmp_path)
    trusted = candidate.sha(path / "manifest.json")
    loaded_manifest, loaded = candidate.load(path, trusted)
    for task, scores in candidate.infer(models, matrix).items():
        np.testing.assert_array_equal(scores, candidate.infer(loaded, matrix, batch=7)[task])
    assert loaded_manifest == manifest
    assert candidate.eligibility(manifest, SimpleNamespace(scoring_mode="synthetic_demo", synthetic=True))[0]
    assert not candidate.eligibility(manifest, SimpleNamespace(scoring_mode="synthetic_demo", synthetic=False))[0]
    assert not candidate.eligibility(manifest, SimpleNamespace(scoring_mode="unsupervised"))[0]
    approved = {**manifest, "eligibility": "validated_candidate", "promotion": {"representative_labels": True, "domain": "test-patterns"}}
    assert not candidate.eligibility(approved, SimpleNamespace(scoring_mode="validated_candidate", candidate_domain="test-patterns"))[0]
    from app.engine.investigations import PROCEDURE_SHA256
    from app.ml.promotion import POLICY
    approved["promotion"]["quality_validation"] = {"policy": POLICY, "status": "PASSED", "reports_sha256": ["test-metadata-only"],
        "grouping_sha256": PROCEDURE_SHA256, "group_capacity": 100, "review_budget": 100,
        "protocol_sha256": "UNIT_TEST_ONLY", "registered_final_ids": ["UNIT_TEST_ONLY"], "frozen_manifest_sha256": trusted,
        **{field: manifest[field] for field in ("release_id", "payload_sha256", "feature_sha256", "feature_contract", "queue_policy")}}
    approved["provenance"] = {**manifest.get("provenance", {}), "parent_manifest_sha256": trusted,
        "protocol_sha256": "UNIT_TEST_ONLY", "registered_final_ids": ["UNIT_TEST_ONLY"], "grouping_sha256": PROCEDURE_SHA256}
    assert candidate.eligibility(approved, SimpleNamespace(scoring_mode="validated_candidate", candidate_domain="test-patterns"))[0]
    assert not candidate.eligibility(approved, SimpleNamespace(scoring_mode="validated_candidate", candidate_domain="other-patterns"))[0]
    with pytest.raises(ValueError, match="externally trusted"):
        candidate.load(path, "0" * 64)
    with (path / "weights.joblib").open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        candidate.load(path, trusted)


def test_new_causal_feature_contract_is_cutoff_invariant():
    from app.ml.facts import facts_from_records
    records = {"transactions": [], "inputs": [], "outputs": []}
    for i in range(8):
        tx = f"{i:064x}"
        records["transactions"].append({"txid": tx, "block_time": f"2026-01-01T00:{i * 10:02d}:00Z" if i < 6 else f"2026-01-01T01:{(i-6)*10:02d}:00Z", "fee_sats": 0})
        records["outputs"].extend({"txid": tx, "vout": j, "amount_sats": 1000, "address": f"a{i}-{j}", "script_type": "p2wpkh"} for j in range(3))
    full = facts_from_records(records)
    small = facts_from_records({key: [row for row in rows if int(row["txid"], 16) < 4] for key, rows in records.items()})
    np.testing.assert_array_equal(candidate.features(full)[:4], candidate.features(small))


def test_evidence_empty_real_counter_stale_coverage_authorization_and_raw_integrity(tmp_path, monkeypatch):
    from test_phase_four import _client

    from app.api import routes
    from workers import runner
    client = _client(tmp_path, monkeypatch)
    owner = {"X-TraceX-Actor": "evidence-owner"}
    case = client.post("/v1/cases", headers=owner, json={"name": "benign shape", "synthetic": True}).json()["case_id"]
    rows = [{"txid": f"{i:064x}", "network": "bitcoin-regtest", "timestamp": f"2026-01-01T00:00:0{i}Z", "inputs": [],
        "outputs": [{"address": f"fund-{i}", "amount_sats": 1000}], "fee_sats": 0} for i in range(3)]
    rows.append({"txid": f"{3:064x}", "network": "bitcoin-regtest", "timestamp": "2026-01-01T00:01:00Z",
        "inputs": [{"prev_txid": f"{i:064x}", "prev_vout": 0, "address": f"fund-{i}", "amount_sats": 1000} for i in range(3)],
        "outputs": [{"address": f"pay-{i}", "amount_sats": 900} for i in range(3)], "fee_sats": 300})
    response = client.post(f"/v1/cases/{case}/imports", headers={**owner, "Idempotency-Key": "tiny-evidence"},
        files={"file": ("small.ndjson", "\n".join(map(json.dumps, rows)), "application/x-ndjson")})
    assert response.status_code == 202
    runner.process_one("tiny-evidence")
    findings = client.get(f"/v1/cases/{case}/findings", headers=owner).json()["findings"]
    assert findings
    item = next((row for row in findings if row["entity_ref"] == "tx:" + f"{3:064x}"), findings[0])
    endpoint = f"/v1/findings/{item['finding_id']}/evidence"
    evidence = client.get(endpoint, headers=owner).json()["structured_evidence"]
    assert evidence["counter_evidence_summary"] == EMPTY_COUNTER
    assert evidence["benign_alternatives"]
    assert client.post(f"/v1/findings/{item['finding_id']}/chat", headers=owner, json={}).status_code == 404
    assert client.get(endpoint, headers={"X-TraceX-Actor": "outsider"}).status_code in {403, 404}
    ref = next(reference for reference in evidence["supporting_refs"] if reference["locator"] == "/3")
    with runner.SessionLocal() as session:
        finding = session.get(FindingRecord, item["finding_id"])
        finding.opposing_evidence = [{"kind": "observed_fee_check", "statement": "The supplied record has a 300 sat fee, consistent with its recorded input/output difference; this weakens an arithmetic-inconsistency interpretation, not the equal-output structural match", "source_refs": [ref]}]
        finding.coverage = {"spend_lineage_complete": False}
        session.commit()
        view = structured_evidence(session, finding, [])
        assert len(view["observed_counter_evidence"]) == 1
        assert any("Missing prevouts" in line for line in view["missing_evidence"])
        finding.opposing_evidence = [{"kind": "recorded_control", "statement": "Stale", "source_refs": [{**ref, "source_sha256": "0" * 64}]}]
        session.commit()
        assert structured_evidence(session, finding, [])["counter_evidence_summary"] == EMPTY_COUNTER
        source = session.get(EvidenceSource, ref["evidence_id"])
        raw = routes.settings.evidence_root / source.storage_relative_path
    replay = f"/v1/evidence/{ref['evidence_id']}/record"
    # Discover exact route through OpenAPI, rather than asserting a nonexistent path.
    replay = next(path for path in client.get("/openapi.json").json()["paths"] if "{source_id}" in path and path.endswith("/records")).replace("{source_id}", ref["evidence_id"])
    assert client.get(replay, headers=owner, params={"locator": ref["locator"]}).status_code == 200
    raw.write_bytes(raw.read_bytes() + b"\n")
    assert client.get(replay, headers=owner, params={"locator": ref["locator"]}).status_code == 409
    assert client.get(replay, headers={"X-TraceX-Actor": "outsider"}, params={"locator": ref["locator"]}).status_code in {403, 404}
    assert client.get(f"/v1/cases/{case}/findings/export.ndjson", headers=owner).status_code == 200


def test_actual_candidate_path_and_v2_fallback_preserve_existing_rows(tmp_path_factory, tmp_path):
    from test_ml_pipeline_integration import _ingest_fresh

    from app.ml.candidate_findings import score_or_fallback
    from app.models import GraphSnapshot, Snapshot
    _, _, _, artifact = fitted(tmp_path)
    sessions, _, case_id, job_id, _, settings = _ingest_fresh(tmp_path_factory, label="candidate-tiny")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        prior = list(session.scalars(select(FindingRecord).where(FindingRecord.rule_version.like("anomaly-stack-%"))))
        assert prior
        case = session.get(Case, case_id)
        case.scoring_mode = "synthetic_demo"
        session.commit()
        configured = replace(settings, candidate_directory=artifact, candidate_manifest_sha256=candidate.sha(artifact / "manifest.json"))
        result, decision = score_or_fallback(session, settings=configured, snapshot=session.get(Snapshot, job.snapshot_id),
            graph=session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == job.snapshot_id)), budget=.01)
        assert decision["status"] == "retained" and result.release_id == "anomaly-stack-v2"
        assert result.written == 0


def test_fresh_import_actually_uses_eligible_candidate_and_corrupt_artifact_falls_back(tmp_path_factory, tmp_path):
    from test_ml_pipeline_integration import _ingest_fresh
    _, _, manifest, artifact = fitted(tmp_path)
    configured = {"candidate_directory": artifact, "candidate_manifest_sha256": candidate.sha(artifact / "manifest.json")}
    sessions, _, _, _, _, _ = _ingest_fresh(tmp_path_factory, label="fresh-candidate-tiny",
        settings_overrides=configured, case_overrides={"scoring_mode": "synthetic_demo"})
    with sessions() as session:
        found = list(session.scalars(select(FindingRecord).where(FindingRecord.rule_version == manifest["release_id"])))
        assert found and all(row.feature_vector["eligibility"] == "synthetic_demo" for row in found)
        assert all(row.source_refs for row in found)
    configured["candidate_manifest_sha256"] = "0" * 64
    sessions, _, _, _, _, _ = _ingest_fresh(tmp_path_factory, label="rejected-candidate-tiny",
        settings_overrides=configured, case_overrides={"scoring_mode": "synthetic_demo"})
    with sessions() as session:
        assert session.scalar(select(FindingRecord).where(FindingRecord.rule_version == "anomaly-stack-v2"))


def test_large_admission_metadata_mac_fallback_and_sanitized_export(tmp_path, monkeypatch):
    from app.resources import global_stage_estimates
    from scripts.scale_benchmark import observed_max
    estimate = global_stage_estimates({"transactions": 3_050_000, "outputs": 8_050_000, "inputs": 4_550_000}, native_bytes=3 << 30)
    assert estimate["ml_joint_bytes"] > estimate["ml_global_bytes"] > 7 << 30
    assert observed_max(None, None) is None
    assert observed_max(None, 5) == 5
    assert macbook_benchmark.architecture("aarch64") == "arm64"
    assert macbook_benchmark.architecture("x86_64") == "amd64"
    clean = sanitize({"token": "sensitive", "nested": {"password": "secret"}, "error": "Bearer sensitive token=sensitive"})
    assert "sensitive" not in json.dumps(clean)
    protocol = tmp_path / "protocol.json"
    assert candidate_lifecycle.main(["register", "--protocol", str(protocol), "--final", str(tmp_path / "fresh-final")]) == 0
    assert json.loads(protocol.read_text())["status"] == "NOT RUN"
    with pytest.raises(FileExistsError):
        candidate_lifecycle.main(["register", "--protocol", str(protocol), "--final", str(tmp_path / "fresh-final")])


def test_per_task_metrics_do_not_call_ap_accuracy_or_small_population_p100():
    labels = {task: np.asarray([0, 1, 0, 1], bool) for task in candidate.TASKS}
    scores = {task: np.asarray([.1, .9, .2, .8]) for task in candidate.TASKS}
    for result in candidate_lifecycle.metrics(scores, labels, budget=2).values():
        assert result["ap"] == 1 and result["p_at_100"] is None
        assert result["p_at_review_budget"] == 1
