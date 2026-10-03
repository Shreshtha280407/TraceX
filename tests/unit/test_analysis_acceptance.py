from __future__ import annotations

import json
import urllib.error

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app
from app.models import FindingRecord, FragmentReceipt, ImportJob
from workers import runner


@pytest.fixture
def system(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(evidence_root=tmp_path / "vault", database_url=str(engine.url), lease_seconds=10,
                        max_upload_bytes=1024 * 1024, event_heartbeat_seconds=1)
    monkeypatch.setattr(routes, "settings", settings)
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    monkeypatch.setenv("TRACEX_WORKERS", "1")
    monkeypatch.setenv("TRACEX_MEMORY_BUDGET_MB", "1024")
    monkeypatch.setenv("TRACEX_GEOIP_DIR", str(tmp_path / "missing-geoip"))

    def session_override():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        yield client, sessions
    app.dependency_overrides.clear()
    engine.dispose()


def upload(client, count=60):
    headers = {"X-TraceX-Actor": "owner"}
    case = client.post("/v1/cases", headers=headers, json={"name": "Analysis", "synthetic": True}).json()["case_id"]
    rows = [{"txid": f"{n + 1:064x}", "timestamp": f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z",
             "inputs": [{"address": "unlinked-input", "amount_sats": 100000}],
             "outputs": [{"address": f"recipient-{n}", "amount_sats": 99000}], "fee_sats": 1000}
            for n in range(count)]
    response = client.post(f"/v1/cases/{case}/imports", headers={**headers, "Idempotency-Key": "one"},
                           files={"file": ("source.ndjson", "\n".join(json.dumps(r) for r in rows).encode())})
    assert response.status_code == 202, response.text
    return case, response.json()["job_id"], headers


def test_degradation_retry_and_receipt_activity_do_not_duplicate(system, monkeypatch):
    client, sessions = system
    from app.ml import findings

    original = findings.materialize_ml_findings

    def broken(*args, **kwargs):
        raise RuntimeError("scorer deliberately failed")

    monkeypatch.setattr(findings, "materialize_ml_findings", broken)
    case, job_id, headers = upload(client)
    assert runner.process_one("stage-worker")
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert job["state"] == "completed" and job["analysis"]["state"] == "degraded"
    stage = {r["name"]: r for r in job["analysis"]["stages"]}
    assert stage["ml_scoring"]["status"] == "failed"
    assert "scorer deliberately failed" in stage["ml_scoring"]["reason"]
    assert stage["geoip"]["status"] == "unavailable"
    assert stage["network_coverage"]["status"] == "incomplete"
    with sessions() as session:
        before = [(r.storage_relative_path, r.sha256, r.record_count) for r in
                  session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id))]
    activity = client.get(f"/v1/cases/{case}/activity", headers=headers).json()
    assert activity["provisional"] is False and activity["total"] == 61
    assert client.get(f"/v1/cases/{case}/analysis", headers={"X-TraceX-Actor": "outsider"}).status_code == 404
    assert client.get(f"/v1/cases/{case}/activity", headers={"X-TraceX-Actor": "outsider"}).status_code == 404
    assert client.post(f"/v1/cases/{case}/analysis/retry?job_id={job_id}", headers={"X-TraceX-Actor": "outsider"}).status_code == 404
    monkeypatch.setattr(findings, "materialize_ml_findings", original)
    assert client.post(f"/v1/cases/{case}/analysis/retry?job_id={job_id}", headers=headers).status_code == 202
    assert runner.process_one("retry-worker")
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert {r["name"]: r for r in job["analysis"]["stages"]}["ml_scoring"]["status"] in {"written", "no_rows_flagged"}
    with sessions() as session:
        after = [(r.storage_relative_path, r.sha256, r.record_count) for r in
                 session.scalars(select(FragmentReceipt).where(FragmentReceipt.job_id == job_id))]
        from app.models import AnalysisStage

        assert len(list(session.scalars(select(AnalysisStage).where(AnalysisStage.job_id == job_id,
                                                    AnalysisStage.name == "ingesting")))) == 1
    assert before == after


def test_top_k_ties_zero_and_case_authorization(system):
    client, sessions = system
    case, job_id, headers = upload(client)
    assert runner.process_one("queue-worker")
    queue = client.get(f"/v1/cases/{case}/review-queue?k=2", headers=headers).json()
    assert queue["queued_transactions"] <= 2
    assert queue["threshold_flagged_transactions"] >= queue["queued_transactions"]
    zero = client.get(f"/v1/cases/{case}/review-queue?fraction=0", headers=headers).json()
    assert zero["capacity"] == 0 and zero["items"] == []
    assert client.get(f"/v1/cases/{case}/review-queue?k=2&fraction=.01", headers=headers).status_code == 422
    assert client.get(f"/v1/cases/{case}/review-queue", headers={"X-TraceX-Actor": "outsider"}).status_code == 404
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        flagged = list(session.scalars(select(FindingRecord).where(FindingRecord.snapshot_id == job.snapshot_id,
                                     FindingRecord.rule_version == "anomaly-stack-v2")))
        for finding in flagged:
            finding.raw_score = 1.0
        session.commit()
    tied = client.get(f"/v1/cases/{case}/review-queue?k=2", headers=headers).json()
    expected = sorted({f.entity_ref for f in flagged})[:2]
    assert [r["transaction"] for r in tied["items"]] == expected


def test_analyst_labels_are_proposition_scoped_and_review_versions_match(system):
    client, sessions = system
    case, job_id, headers = upload(client)
    assert runner.process_one("label-worker")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        finding = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == job.snapshot_id,
                                                           FindingRecord.entity_ref.startswith("tx:")))
        assert finding is not None
        fields = {column.name: getattr(finding, column.name) for column in FindingRecord.__table__.columns
                  if column.name not in {"id", "created_at", "updated_at"}}
        second = FindingRecord(**{**fields, "rule_id": "other-proposition", "claim": "Different proposition"})
        session.add(second)
        session.commit()
        original_id, second_id, snapshot = finding.id, second.id, job.snapshot_id
    for identity, decision in ((original_id, "dismissed"), (second_id, "triaged")):
        response = client.post(f"/v1/findings/{identity}/reviews", headers=headers,
                               json={"expected_finding_version": 1, "disposition": decision, "reason": "Proposition-specific review"})
        assert response.status_code == 201, response.text
    contract = client.get(f"/v1/cases/{case}/analyst-label-contract", headers=headers,
                          params={"snapshot_id": snapshot}).json()
    assert len(contract["records"]) == 2 and contract["negative"] == 1 and contract["unknown"] == 1
    assert len({r["transaction"] for r in contract["records"]}) == 1
    assert all(r["reviewed_finding_version"] == 1 for r in contract["records"])
    assert contract["training_eligible"] is False and contract["positive"] == 0
    assert client.get(f"/v1/cases/{case}/analyst-label-contract", headers={"X-TraceX-Actor": "outsider"},
                      params={"snapshot_id": snapshot}).status_code == 404


def test_confirmed_proposition_and_historical_cutoff_survive_later_reviews(system):
    from datetime import UTC, datetime, timedelta

    from app.models import ReviewDecisionRecord

    client, sessions = system
    case, job_id, headers = upload(client)
    assert runner.process_one("confirm-worker")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        finding = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == job.snapshot_id,
                                                           FindingRecord.entity_ref.startswith("tx:")))
        identity, feature_hash, snapshot_id = finding.id, finding.feature_vector_hash, job.snapshot_id
    for version, disposition in enumerate(("dismissed", "confirmed", "triaged"), start=1):
        response = client.post(f"/v1/findings/{identity}/reviews", headers=headers,
            json={"expected_finding_version": version, "disposition": disposition,
                  "reason": "Review this pattern proposition only, never criminality"})
        assert response.status_code == 201, response.text
    first = datetime(2026, 5, 1, tzinfo=UTC)
    with sessions() as session:
        rows = session.scalars(select(ReviewDecisionRecord).where(ReviewDecisionRecord.finding_id == identity)
                               .order_by(ReviewDecisionRecord.finding_version)).all()
        for index, row in enumerate(rows):
            row.created_at = first + timedelta(minutes=index)
        session.commit()
    endpoint = f"/v1/cases/{case}/analyst-label-contract"
    old = client.get(endpoint, headers=headers, params={"snapshot_id": snapshot_id, "cutoff": first.isoformat()}).json()
    confirmed = client.get(endpoint, headers=headers, params={"snapshot_id": snapshot_id, "cutoff": (first + timedelta(minutes=1)).isoformat()}).json()
    latest = client.get(endpoint, headers=headers, params={"snapshot_id": snapshot_id}).json()
    assert old["negative"] == 1 and old["records"][0]["reviewed_finding_version"] == 1
    assert confirmed["positive"] == 1 and confirmed["records"][0]["reviewed_finding_version"] == 2
    assert latest["unknown"] == 1 and not latest["training_eligible"]
    assert confirmed["records"][0]["feature_vector_hash"] == feature_hash
    assert client.get(f"/v1/cases/{case}/review-queue?review_state=confirmed", headers=headers).status_code == 200


def test_graph_continuation_has_no_duplicates_and_rejects_tampering(system):
    client, _ = system
    case, _, headers = upload(client, 3)
    assert runner.process_one("graph-worker")
    params = {"seed": "tx:" + f"{1:064x}", "depth": 2, "node_limit": 2, "edge_limit": 1}
    first = client.get(f"/v1/cases/{case}/graph", headers=headers, params=params).json()
    assert first["cursor"]
    nodes, edges = set(), set()
    page = first
    for _ in range(20):
        ids, eids = {n["id"] for n in page["nodes"]}, {e["id"] for e in page["edges"]}
        assert not nodes & ids and not edges & eids
        nodes |= ids
        edges |= eids
        if page["cursor"] is None:
            break
        page = client.get(f"/v1/cases/{case}/graph", headers=headers, params={**params, "cursor": page["cursor"]}).json()
    else:
        pytest.fail("cursor did not terminate")
    full = client.get(f"/v1/cases/{case}/graph", headers=headers,
                      params={**params, "node_limit": 1000, "edge_limit": 3000}).json()
    assert nodes == {n["id"] for n in full["nodes"]} and edges == {e["id"] for e in full["edges"]}
    assert client.get(f"/v1/cases/{case}/graph", headers=headers,
                      params={**params, "cursor": first["cursor"] + "broken"}).status_code == 422
    assert client.get(f"/v1/cases/{case}/graph", headers=headers,
                      params={**params, "cursor": first["cursor"], "depth": 3}).status_code == 422
    assert client.get(f"/v1/cases/{case}/graph", headers={"X-TraceX-Actor": "outsider"}, params=params).status_code == 404


def test_calibration_mismatch_and_pinned_legacy_behavior(system, monkeypatch):
    from app.engine import confidence
    from app.models import FindingConfidence

    assert confidence.confidence_for("anomaly_stack_rank", "anomaly-stack-v1", 3, {})["value"] is None
    assert confidence.confidence_for("anomaly_stack_rank", "anomaly-stack-v2", 3, {}, contract="wrong")["value"] is None
    client, sessions = system
    _case, job_id, _headers = upload(client)
    assert runner.process_one("pin-worker")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        finding = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == job.snapshot_id))
        assert finding is not None
        original = confidence.finding_confidence(session, finding)
        monkeypatch.setattr(confidence, "_calibration", lambda: {"rules": {}, "calibration_id": "different"})
        assert confidence.finding_confidence(session, finding) == original
        session.delete(session.get(FindingConfidence, finding.id))
        session.flush()
        legacy = confidence.finding_confidence(session, finding)
        assert legacy["value"] is None and "legacy" in legacy["basis"]


def test_confidence_antijoin_repins_only_missing_rows_and_is_idempotent(system):
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql

    from app.engine.confidence import pin_snapshot_confidence
    from app.models import FindingConfidence

    client, sessions = system
    _case, job_id, _headers = upload(client)
    assert runner.process_one("anti-join-worker")
    with sessions() as session:
        snapshot_id = session.get(ImportJob, job_id).snapshot_id
        before = {p.finding_id: p.provenance for p in session.scalars(select(FindingConfidence))}
        assert before
        missing = next(iter(before))
        session.delete(session.get(FindingConfidence, missing))
        session.flush()
        queries = []

        def capture(_connection, _cursor, statement, _parameters, context, _many):
            if statement.lstrip().startswith("SELECT") and "findings.rule_id" in statement:
                queries.append(str(context.compiled.statement.compile(dialect=postgresql.dialect())))

        event.listen(session.get_bind(), "before_cursor_execute", capture)
        try:
            pin_snapshot_confidence(session, snapshot_id)
            after = {p.finding_id: p.provenance for p in session.scalars(select(FindingConfidence))}
            assert after == before  # Existing pins are not reinterpreted or overwritten.
            pin_snapshot_confidence(session, snapshot_id)
            assert {p.finding_id: p.provenance for p in session.scalars(select(FindingConfidence))} == before
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", capture)
        assert len(queries) == 2
        assert all("EXISTS" in query and "NOT IN" not in query for query in queries)
        assert all("finding_confidence.finding_id = findings.id" in query for query in queries)


def test_benchmark_failure_persists_result_and_refuses_reuse(tmp_path, monkeypatch):
    from scripts import scale_benchmark as benchmark

    monkeypatch.setattr(benchmark, "REPO", tmp_path)
    assert benchmark.main([str(tmp_path / "missing.ndjson"), "--name", "failed-source"]) == 1
    result = tmp_path / "var/scale-run/failed-source/result.json"
    before = result.read_bytes()
    assert json.loads(before)["status"] == "failed"
    with pytest.raises(FileExistsError):
        benchmark.main([str(tmp_path / "missing.ndjson"), "--name", "failed-source"])
    assert result.read_bytes() == before


def test_benchmark_never_retries_authorization_errors(monkeypatch):
    from scripts import scale_benchmark as benchmark

    calls = []

    def unauthorized(*args, **kwargs):
        calls.append(True)
        raise urllib.error.HTTPError("http://localhost", 403, "Forbidden", {}, None)

    monkeypatch.setattr(benchmark.urllib.request, "urlopen", unauthorized)
    with pytest.raises(urllib.error.HTTPError):
        benchmark._get("http://localhost", "/job", {}, attempts=10)
    assert len(calls) == 1


def test_completed_job_does_not_pass_with_missing_mandatory_analysis():
    from scripts.scale_benchmark import acceptance_errors

    errors = acceptance_errors({"state": "completed", "rows_seen": 1, "rows_accepted": 1, "rows_quarantined": 0},
                               {}, required_stages=["ml_scoring", "analytics"])
    assert any("mandatory stage ml_scoring" in e for e in errors)
    assert "analytics snapshot missing" in errors and "offline Geo-IP database missing" in errors


def test_geoip_retry_creates_revision_without_mutating_receipts_or_confidence(system, monkeypatch):
    import hashlib
    from pathlib import Path

    from app.engine import geoip
    from app.engine.analytics import latest_analytics
    from app.models import AnalyticsRevision, AnalyticsSnapshot, FindingConfidence, GraphSnapshot

    client, sessions = system
    case, job_id, headers = upload(client)
    assert runner.process_one("before-geoip")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        original = session.scalar(select(AnalyticsSnapshot).where(AnalyticsSnapshot.snapshot_id == job.snapshot_id))
        assert not original.summary["geoip_installed"]
        original_id, original_path, original_sha = original.id, original.storage_relative_path, original.sha256
        graph = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == job.snapshot_id))
        graph_sha = graph.sha256
        receipt_hashes = sorted(session.scalars(select(FragmentReceipt.sha256).where(FragmentReceipt.job_id == job_id)))
        pinned = {row.finding_id: row.provenance for row in session.scalars(select(FindingConfidence))}
    geo_dir = Path(runner.settings.evidence_root).parent / "installed-geoip"
    country = geo_dir.parent / "dbip-country-test.csv"
    country.write_text("1.0.0.0,1.0.0.255,AU\n")
    geoip.compile_database([country], geo_dir)
    monkeypatch.setenv("TRACEX_GEOIP_DIR", str(geo_dir))
    assert client.post(f"/v1/cases/{case}/analysis/retry?job_id={job_id}", headers=headers).status_code == 202
    assert runner.process_one("after-geoip")
    with sessions() as session:
        current = latest_analytics(session, case, job.snapshot_id)
        assert isinstance(current, AnalyticsRevision) and current.revision == 1
        assert current.summary["geoip_installed"] and current.summary["supersedes_analytics_id"] == original_id
        original = session.get(AnalyticsSnapshot, original_id)
        assert not original.summary["geoip_installed"] and original.sha256 == original_sha
        assert hashlib.sha256((runner.settings.evidence_root / original_path).read_bytes()).hexdigest() == original_sha
        assert session.get(GraphSnapshot, graph.id).sha256 == graph_sha
        assert sorted(session.scalars(select(FragmentReceipt.sha256).where(FragmentReceipt.job_id == job_id))) == receipt_hashes
        assert {row.finding_id: row.provenance for row in session.scalars(select(FindingConfidence))} == pinned
    status = client.get(f"/v1/cases/{case}/analysis", headers=headers).json()
    assert status["analytics"]["revision"] == 1 and status["analytics"]["geoip_installed"]
    # Explicit refresh also uses the same authorized, durable queue, never an upload.
    uri = f"/v1/cases/{case}/analysis/retry?job_id={job_id}&recompute_analytics=true"
    assert client.post(uri, headers={"X-TraceX-Actor": "outsider"}).status_code == 404
    assert client.post(uri, headers=headers).status_code == 202
    assert client.post(uri, headers=headers).status_code == 409
    assert runner.process_one("explicit-refresh")
    with sessions() as session:
        assert latest_analytics(session, case, job.snapshot_id).revision == 2


def test_invalid_benchmark_resume_persists_failure(tmp_path, monkeypatch):
    from scripts import scale_benchmark as benchmark

    monkeypatch.setattr(benchmark, "REPO", tmp_path)
    assert benchmark.main([str(tmp_path / "missing"), "--name", "bad-resume",
                           "--resume-from", str(tmp_path / "missing-run")]) == 1
    report = json.loads((tmp_path / "var/scale-run/bad-resume/result.json").read_text())
    assert report["status"] == "failed" and "FileNotFoundError" in report["error"]


def test_explicit_refresh_intent_survives_a_failed_attempt(system, monkeypatch):
    from app.engine.analytics import latest_analytics
    from app.engine.ingestion import pipeline
    from app.models import AnalysisRequest

    client, sessions = system
    case, job_id, headers = upload(client)
    assert runner.process_one("original")
    original = pipeline._materialize_analytics
    monkeypatch.setattr(pipeline, "_materialize_analytics", lambda *args, **kwargs: {
        "status": "failed", "reason": "simulated interruption", "network_findings": 0})
    uri = f"/v1/cases/{case}/analysis/retry?job_id={job_id}"
    assert client.post(uri + "&recompute_analytics=true", headers=headers).status_code == 202
    assert runner.process_one("interrupted-refresh")
    with sessions() as session:
        request = session.get(AnalysisRequest, (job_id, 2))
        assert request.refresh_analytics and not request.fulfilled
    monkeypatch.setattr(pipeline, "_materialize_analytics", original)
    assert client.post(uri, headers=headers).status_code == 202
    assert runner.process_one("resumed-refresh")
    with sessions() as session:
        assert latest_analytics(session, case).revision == 1
        assert session.get(AnalysisRequest, (job_id, 2)).fulfilled
