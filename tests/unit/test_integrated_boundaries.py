"""Small ranking/parity and failure-boundary checks (no large fixtures)."""
import json

import numpy as np
import pytest
from sqlalchemy import select
from test_analysis_acceptance import system as system  # noqa: PLC0414 - pytest fixture re-export
from test_analysis_acceptance import upload
from threadpoolctl import threadpool_limits

from app.ml import candidate, grains, layers
from app.ml.facts import load_facts
from app.models import FindingRecord, ImportJob
from scripts import appliance_acceptance, quality_matrix
from scripts.export_review_package import main as export_results
from scripts.scale_benchmark import acceptance_errors


def test_rank_scales_family_round_robin_and_context_are_independent(system):
    from workers import runner
    client, sessions = system
    case, job_id, headers = upload(client)
    runner.process_one("rank-tiny")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        original = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == job.snapshot_id))
        fields = {column.name: getattr(original, column.name) for column in FindingRecord.__table__.columns if column.name not in {"id", "created_at", "updated_at"}}
        for rule, values in (("a_scale_100", [100, 99]), ("b_scale_1", [1, .9])):
            for index, score in enumerate(values):
                session.add(FindingRecord(**{**fields, "rule_id": rule, "rule_version": "ranking-fixture-v1", "raw_score": score, "entity_ref": f"tx:rank-{rule}-{index}", "status": "open"}))
        session.commit()
    params = [("rule_id", "a_scale_100"), ("rule_id", "b_scale_1")]
    rows = client.get(f"/v1/cases/{case}/findings", params=params, headers=headers).json()["findings"]
    assert [row["family_rank"] for row in rows] == [1, 1, 2, 2]
    assert [row["raw_score"] for row in rows] == [100, 1, 99, .9]
    with sessions() as session:
        session.get(FindingRecord, rows[-1]["finding_id"]).status = "escalated"
        session.get(FindingRecord, rows[0]["finding_id"]).status = "dismissed"
        session.commit()
    response = client.get(f"/v1/cases/{case}/findings", params=params, headers=headers).json()
    assert response["findings"][0]["status"] == "escalated"
    assert response["findings"][-1]["status"] == "dismissed"
    assert "not calibrated" in response["review_policy"]["meaning"]
    filtered = client.get(f"/v1/cases/{case}/findings", params=[*params, ("review_state", "dismissed")], headers=headers).json()
    assert filtered["total"] == 1


def test_shape_label_optimization_and_immutable_index_cache_parity(tmp_path):
    quality_matrix.independent_fixture(tmp_path / "data", 64, 25, "parity")
    facts = load_facts(tmp_path / "data")
    mutable = facts.outputs_of()
    facts.out_tx[0] = 1
    changed = facts.outputs_of()
    assert not np.array_equal(mutable[0], changed[0])
    facts.out_tx[0] = 0
    facts.freeze_indexes()
    assert facts.outputs_of()[0] is facts.outputs_of()[0]
    with pytest.raises(ValueError):
        facts.out_tx[0] = 1
    table = grains.build_transaction_table(facts)
    reference = np.ones(64, dtype=bool)
    with threadpool_limits(limits=1):
        old = layers.layer_a_structure(table, reference).detail["shape_family"]
        new = layers.shape_families(table, reference)
    np.testing.assert_array_equal(old, new)


def test_strict_under_1800_gate_and_compact_results_are_underlying_sanitized_data(tmp_path):
    errors = acceptance_errors({"state": "completed", "rows_seen": 1, "rows_accepted": 1, "rows_quarantined": 0}, {},
        required_stages=[], require_geoip=False, max_seconds=1800, seconds=1800)
    assert any("less than" in error for error in errors)
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"status": "NOT RUN", "acceptance_errors": [], "results": {"motif": {"ap": None}}, "token": "never-export", "reproduction": ["python -m scripts.macbook_benchmark accept --help"]}))
    destination = tmp_path / "compact"
    export_results(["--result", str(source), "--output", str(destination)])
    exported = (destination / "result-00.json").read_text()
    assert "never-export" not in exported and '"ap": null' in exported
    assert json.loads(exported)["dirty_tree"] is None  # Unknown provenance is not a clean-tree claim.
    assert json.loads((destination / "inventory.json").read_text())[0]["sha256"] == candidate.sha(destination / "result-00.json")


def test_final_findings_timeout_cannot_pass_and_diagnostics_survive(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(appliance_acceptance, "REPO", tmp_path)
    source = tmp_path / "tiny.ndjson"
    source.write_text("{}\n")
    monkeypatch.setattr(appliance_acceptance, "_environment", lambda *a: {"runtime_source_sha256": {}})
    class DummyThread:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def join(self, **kwargs): pass
    monkeypatch.setattr(appliance_acceptance.threading, "Thread", DummyThread)
    def output(command, **kwargs):
        if "inspect" in command:
            return json.dumps([{"Image": "image", "HostConfig": {"Memory": 0, "NanoCpus": 0}}])
        if "python" in command:
            return json.dumps({"runtime_source_sha256": {}})
        return "container-id"
    monkeypatch.setattr(appliance_acceptance.subprocess, "check_output", output)
    monkeypatch.setattr(appliance_acceptance.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="diagnostic", stderr="", returncode=0))
    monkeypatch.setattr(appliance_acceptance, "upload", lambda *a, **k: {"job_id": "job"})
    def request(base, path, **kwargs):
        if path.endswith("signup"): return {"token": "private"}
        if path == "/cases": return {"case_id": "case"}
        if path.startswith("/jobs/"): return {"state": "completed", "stage": "ingested", "rows_seen": 1}
        raise TimeoutError("final findings unavailable")
    monkeypatch.setattr(appliance_acceptance, "request", request)
    assert appliance_acceptance.main(["--base", "http://localhost:1", "--compose", str(tmp_path / "compose"), "--project", "tiny", "--source", str(source), "--expected-counts", str(tmp_path / "counts"), "--name", "timeout"]) == 1
    result = json.loads((tmp_path / "var/appliance-runs/timeout/result.json").read_text())
    assert result["exit_code"] == 1 and "final findings unavailable" in result["error"]
    assert (tmp_path / "var/appliance-runs/timeout/worker.log").exists()


def test_missing_api_is_404_with_the_spa_served_for_all_methods(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import main
    (tmp_path / "index.html").write_text("<html>fixture</html>")
    isolated = FastAPI()
    monkeypatch.setattr(main, "app", isolated)
    main._mount_web_ui(str(tmp_path))
    with TestClient(isolated) as client:
        for method in ("GET", "POST", "PUT", "DELETE"):
            assert client.request(method, "/v1/removed-api-resource").status_code == 404
        assert client.get("/cases/fixture/findings").status_code == 200


def test_successful_zero_finding_stage_pins_scorer_across_retry(system, monkeypatch):
    from app.ml import findings
    from app.ml.candidate_findings import score_or_fallback
    from app.models import Case, Snapshot
    from workers import runner
    client, sessions = system
    monkeypatch.setattr(findings, "materialize_ml_findings", lambda *a, **k: findings.MLFindingResult(0, 60, ("A_global", "D_burst"), .01, 0, release_id="anomaly-stack-v2"))
    case_id, job_id, _ = upload(client)
    runner.process_one("zero-flagged")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        case = session.get(Case, case_id)
        case.scoring_mode = "synthetic_demo"
        session.commit()
        result, decision = score_or_fallback(session, settings=runner.settings,
            snapshot=session.get(Snapshot, job.snapshot_id), graph=None, budget=.01)
        assert result.release_id == "anomaly-stack-v2" and result.written == 0
        assert decision["status"] == "retained" and "zero-row" in decision["reason"]


def test_candidate_pattern_probability_is_not_a_gaussian_anomaly_tail():
    from app.engine.confidence import confidence_for
    result = confidence_for("candidate_pattern_triage", "anomaly-stack-candidate-v1-tiny", .8, {"task_scores": {"motif": .8}})
    assert "anomaly_p_value" not in result


def test_bounded_similarity_chunk_topk_matches_stable_global_order(tmp_path, monkeypatch):
    from app.engine import analytics
    # Tiny 72 KiB projection spanning two chunks; NOT transactions or a large study.
    matrix = np.zeros((9000, 2), dtype=np.float32)
    matrix[:, 0] = 1
    matrix[-1, 0] = 2
    class Cursor:
        closed = False
        def execute(self, query, params=None):
            self.params = params
            return self
        def fetchone(self): return (0,)
        def fetchall(self): return [(row, f"wallet-{row}") for row in self.params]
        def close(self): self.closed = True
    cursor = Cursor()
    monkeypatch.setattr(analytics, "cursor_for", lambda *a: cursor)
    monkeypatch.setattr(analytics, "_table_exists", lambda *a: True)
    monkeypatch.setattr(analytics, "_embedding_matrix", lambda *a: matrix)
    result = analytics.similar_wallets(tmp_path, None, "E-fixture", limit=10)
    expected = [8999, *range(1, 10)]
    assert [row["wallet"] for row in result["similar"]] == [f"wallet-{row}" for row in expected]
    assert cursor.closed and "embedding-row-ascending-v1" in result["tie_policy"]
