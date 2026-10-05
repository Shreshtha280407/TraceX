"""Small fixtures only: legacy queue recovery never reimports or rescores."""
import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.engine import grouping_materializer, investigations
from app.models import (
    AnalysisRequest,
    AnalysisStage,
    FindingConfidence,
    FindingRecord,
    FragmentReceipt,
    GraphSnapshot,
    ImportJob,
    InvestigationGroup,
    InvestigationMember,
    InvestigationRun,
)
from tests.unit.test_analysis_acceptance import system as analysis_system
from tests.unit.test_analysis_acceptance import upload
from workers import runner


@pytest.fixture
def system(tmp_path, monkeypatch):
    yield from analysis_system.__wrapped__(tmp_path, monkeypatch)


def legacy_case(system, monkeypatch):
    client, sessions = system
    original = grouping_materializer.materialize
    # Simulate a pre-grouping release on sixty real canonical transactions,
    # without deleting any existing owner case, dataset or evidence.
    monkeypatch.setattr(grouping_materializer, "materialize", lambda *args, **kwargs: {"legacy_fixture": True})
    case, job_id, headers = upload(client, 60)
    assert runner.process_one("legacy-fixture")
    monkeypatch.setattr(grouping_materializer, "materialize", original)
    with sessions() as session:
        session.execute(delete(AnalysisStage).where(AnalysisStage.job_id == job_id,
                                                   AnalysisStage.name == "investigation_grouping"))
        session.commit()
    return client, sessions, case, job_id, headers


def forbid_full_pipeline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("group-only recovery must not reimport, rebuild the graph or rerun ML/analytics")
    monkeypatch.setattr(runner, "ingest_source", forbidden)


def test_legacy_group_build_is_authorized_idempotent_and_preserves_evidence_reviews_and_stages(system, monkeypatch):
    client, sessions, case, job_id, headers = legacy_case(system, monkeypatch)
    endpoint = f"/v1/cases/{case}/investigation-queue"
    before = client.get(endpoint, headers=headers).json()
    assert before["underlying_findings"] > 0 and before["queued_groups"] == 0
    assert before["grouping_coverage"]["state"] == "incomplete"
    assert before["grouping_coverage"]["active_jobs"] == 0
    with sessions() as session:
        receipts = {r.id: (r.sha256, r.record_count) for r in session.scalars(select(FragmentReceipt))}
        pins = copy.deepcopy({p.finding_id: p.provenance for p in session.scalars(select(FindingConfidence))})
        stages = {s.id: (s.status, s.duration_seconds, copy.deepcopy(s.details)) for s in session.scalars(
            select(AnalysisStage).where(AnalysisStage.name != "source_verification"))}
        graph = session.scalar(select(GraphSnapshot))
        graph_identity = (graph.id, graph.sha256, graph.storage_relative_path)
        finding = session.scalar(select(FindingRecord))
        finding_id = finding.id
    review = client.post(f"/v1/findings/{finding_id}/reviews", headers=headers,
                         json={"expected_finding_version": 1, "disposition": "dismissed",
                               "reason": "Preserve this finding's independent proposition review"})
    assert review.status_code == 201, review.text
    assert client.post(endpoint + "/build", headers={"X-TraceX-Actor": "outsider"}).status_code == 404
    assert client.post(endpoint + "/build?limit=21", headers=headers).status_code == 422
    for _ in range(2):
        result = client.post(endpoint + "/build", headers=headers)
        assert result.status_code == 202, result.text
        assert result.json()["grouping_only"] and [j["job_id"] for j in result.json()["jobs"]] == [job_id]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRequest)) == 1
    assert client.get(endpoint, headers=headers).json()["grouping_coverage"]["active_jobs"] == 1
    forbid_full_pipeline(monkeypatch)
    assert runner.process_one("groups-only")
    after = client.get(endpoint, headers=headers).json()
    assert after["underlying_findings"] == before["underlying_findings"]
    assert after["grouping_coverage"]["state"] == "complete"
    assert after["queued_groups"] == min(100, after["unresolved_groups"]) > 0
    assert after["backlog_groups"] == max(0, after["unresolved_groups"] - 100)
    with sessions() as session:
        assert session.get(AnalysisRequest, (job_id, 2)).fulfilled
        assert {r.id: (r.sha256, r.record_count) for r in session.scalars(select(FragmentReceipt))} == receipts
        assert {p.finding_id: p.provenance for p in session.scalars(select(FindingConfidence))} == pins
        for identity, original in stages.items():
            stage = session.get(AnalysisStage, identity)
            assert (stage.status, stage.duration_seconds, stage.details) == original
        graph = session.scalar(select(GraphSnapshot))
        assert (graph.id, graph.sha256, graph.storage_relative_path) == graph_identity
        finding = session.get(FindingRecord, finding_id)
        assert finding.status == "dismissed" and finding.finding_version == 2
        assert session.get(ImportJob, job_id).rows_accepted == 60
    # Recovery does not certify missing/insufficient old ML or Geo-IP stages.
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).json()["analysis"]["state"] == "degraded"
    group = client.get(f"/v1/investigation-groups/{after['items'][0]['group_id']}/members", headers=headers)
    assert group.status_code == 200 and group.json()["items"]
    ref = group.json()["items"][0]["finding"]["source_refs"][0]
    assert client.get(f"/v1/evidence/{ref['evidence_id']}/records", headers=headers,
                      params={"locator": ref["locator"]}).status_code == 200
    assert client.post(endpoint + "/build", headers=headers).json()["jobs"] == []


def test_pending_count_is_capped_at_100_real_groups_with_accessible_backlog(system, monkeypatch):
    client, sessions, case, _job_id, headers = legacy_case(system, monkeypatch)
    with sessions() as session:
        original = session.scalar(select(FindingRecord))
        fields = {column.name: copy.deepcopy(getattr(original, column.name))
                  for column in FindingRecord.__table__.columns
                  if column.name not in {"id", "created_at", "updated_at"}}
        # Count/group metadata on sixty canonical transactions, not a 100K fixture.
        for n in range(105):
            session.add(FindingRecord(**{**fields, "entity_ref": f"addr:independent-focal-{n:03d}"}))
        session.commit()
    endpoint = f"/v1/cases/{case}/investigation-queue"
    assert client.post(endpoint + "/build", headers=headers).status_code == 202
    forbid_full_pipeline(monkeypatch)
    assert runner.process_one("capacity-groups")
    for capacity in (0, 1, 100):
        queue = client.get(endpoint, headers=headers, params={"capacity": capacity}).json()
        assert queue["queued_groups"] == capacity
        assert queue["backlog_groups"] == queue["unresolved_groups"] - capacity
        assert len(queue["items"]) <= 20
    queue = client.get(endpoint, headers=headers).json()
    assert queue["unresolved_groups"] > 100
    backlog = client.get(endpoint, headers=headers, params={"scope": "backlog"}).json()
    assert backlog["items"] and backlog["filtered_total"] == queue["backlog_groups"]
    gid = queue["items"][0]["group_id"]
    assert client.post(f"/v1/investigation-groups/{gid}/reviews", headers=headers,
        json={"expected_review_version": 1, "disposition": "triaged", "reason": "Group proposition triaged"}).status_code == 201
    after = client.get(endpoint, headers=headers).json()
    assert after["queued_groups"] == 100 and after["backlog_groups"] == queue["backlog_groups"] - 1
    assert after["underlying_findings"] == queue["underlying_findings"]


def test_group_recovery_failure_retains_intent_and_can_retry_without_full_analysis(system, monkeypatch):
    client, sessions, case, job_id, headers = legacy_case(system, monkeypatch)
    original = investigations.observations
    def broken(*args):
        raise RuntimeError("grouping-only fixture interruption")
    monkeypatch.setattr(investigations, "observations", broken)
    endpoint = f"/v1/cases/{case}/investigation-queue"
    assert client.post(endpoint + "/build", headers=headers).status_code == 202
    forbid_full_pipeline(monkeypatch)
    assert runner.process_one("group-recovery-fail")
    with sessions() as session:
        assert session.get(ImportJob, job_id).state == "failed"
        assert not session.get(AnalysisRequest, (job_id, 2)).fulfilled
        assert session.scalar(select(func.count()).select_from(InvestigationRun)) == 0
    monkeypatch.setattr(investigations, "observations", original)
    assert client.post(endpoint + "/build", headers=headers).status_code == 202
    assert runner.process_one("group-recovery-retry")
    with sessions() as session:
        assert session.get(ImportJob, job_id).state == "completed"
        assert session.get(AnalysisRequest, (job_id, 2)).fulfilled
        assert session.get(AnalysisRequest, (job_id, 3)).fulfilled
    assert client.get(endpoint, headers=headers).json()["queued_groups"] > 0


def test_group_recovery_survives_a_worker_crash_without_losing_intent(system, monkeypatch):
    client, sessions, case, job_id, headers = legacy_case(system, monkeypatch)
    original = investigations.observations
    def killed(*args):
        raise KeyboardInterrupt("simulated hard kill during group recovery")
    monkeypatch.setattr(investigations, "observations", killed)
    assert client.post(f"/v1/cases/{case}/investigation-queue/build", headers=headers).status_code == 202
    forbid_full_pipeline(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        runner.process_one("group-recovery-killed")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job.state == "running" and not session.get(AnalysisRequest, (job_id, 2)).fulfilled
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    monkeypatch.setattr(investigations, "observations", original)
    assert runner.process_one("group-recovery-reclaimed")
    with sessions() as session:
        assert session.get(ImportJob, job_id).state == "completed"
        assert session.get(AnalysisRequest, (job_id, 2)).fulfilled


def test_group_recovery_requires_immutable_canonical_graph(system, monkeypatch):
    client, sessions, case, job_id, headers = legacy_case(system, monkeypatch)
    with sessions() as session:
        graph = session.scalar(select(GraphSnapshot))
        original = graph.sha256
        graph.sha256 = "0" * 64
        session.commit()
    assert client.post(f"/v1/cases/{case}/investigation-queue/build", headers=headers).status_code == 202
    forbid_full_pipeline(monkeypatch)
    assert runner.process_one("tampered-graph")
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert job["state"] == "failed" and "immutable canonical graph hash mismatch" in job["error_detail"]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(InvestigationMember)) == 0
        assert not session.get(AnalysisRequest, (job_id, 2)).fulfilled
        session.scalar(select(GraphSnapshot)).sha256 = original
        session.commit()


def test_full_analysis_request_supersedes_group_only_recovery(system, monkeypatch):
    client, sessions, case, job_id, headers = legacy_case(system, monkeypatch)
    original = investigations.observations
    def broken(*args):
        raise RuntimeError("failed group-only request")
    monkeypatch.setattr(investigations, "observations", broken)
    assert client.post(f"/v1/cases/{case}/investigation-queue/build", headers=headers).status_code == 202
    assert runner.process_one("group-only-fail")
    monkeypatch.setattr(investigations, "observations", original)
    assert client.post(f"/v1/cases/{case}/analysis/retry?job_id={job_id}", headers=headers).status_code == 202
    original_ingest = runner.ingest_source
    called = []
    def full_analysis(*args, **kwargs):
        called.append(kwargs["job"].id)
        return original_ingest(*args, **kwargs)
    monkeypatch.setattr(runner, "ingest_source", full_analysis)
    assert runner.process_one("explicit-full-retry")
    assert called == [job_id]
    with sessions() as session:
        assert session.get(AnalysisRequest, (job_id, 2)).fulfilled
        assert session.get(ImportJob, job_id).state == "completed"


def test_repair_reuses_identical_membership_and_keeps_group_decisions(system, monkeypatch):
    client, sessions = system
    case, job_id, headers = upload(client, 60)
    assert runner.process_one("reviewed-groups-fixture")
    endpoint = f"/v1/cases/{case}/investigation-queue"
    queue = client.get(endpoint, headers=headers).json()
    group = queue["items"][0]
    gid = group["group_id"]
    assert client.post(f"/v1/investigation-groups/{gid}/reviews", headers=headers,
        json={"expected_review_version": 1, "disposition": "triaged", "reason": "Preserve reviewed proposition"}).status_code == 201
    with sessions() as session:
        members = list(session.scalars(select(InvestigationMember.finding_id).where(InvestigationMember.group_id == gid)))
        session.get(InvestigationRun, group["run_id"]).active = False
        session.commit()
    assert client.post(endpoint + "/build", headers=headers).status_code == 202
    forbid_full_pipeline(monkeypatch)
    assert runner.process_one("reactivate-identical-groups")
    with sessions() as session:
        assert session.get(InvestigationRun, group["run_id"]).active
        assert session.get(InvestigationGroup, gid).status == "triaged"
        assert list(session.scalars(select(InvestigationMember.finding_id).where(InvestigationMember.group_id == gid))) == members
        assert session.get(ImportJob, job_id).state == "completed"
    after = client.get(endpoint, headers=headers).json()
    assert after["unresolved_groups"] == queue["unresolved_groups"] - 1
