from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from sqlalchemy import func, select

from app.engine import investigations
from app.models import (
    FindingRecord,
    GraphSnapshot,
    ImportJob,
    InvestigationGroup,
    InvestigationMember,
    InvestigationReplacement,
    InvestigationReview,
    InvestigationRun,
    ReviewDecisionRecord,
)
from tests.unit.test_analysis_acceptance import system as analysis_system
from tests.unit.test_analysis_acceptance import upload
from workers import runner


@pytest.fixture
def system(tmp_path, monkeypatch):
    yield from analysis_system.__wrapped__(tmp_path, monkeypatch)


def fixture_groups(system, monkeypatch):
    client, sessions = system
    case, job_id, headers = upload(client, 3)
    assert runner.process_one("group-fixture")
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        graph = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == job.snapshot_id))
        # Separate tiny graph for explicit verified UTXO connections; no assertions
        # based on shared IP, scenario IDs, labels or common-input ownership.
        path = runner.settings.evidence_root / "group-fixture.duckdb"
        with duckdb.connect(str(path)) as db:
            db.execute("CREATE TABLE edges(edge_id VARCHAR, from_node VARCHAR, to_node VARCHAR, edge_type VARCHAR)")
            db.execute("INSERT INTO edges VALUES ('e1','out:a:0','tx:b','SPENT_BY'),('e2','out:b:0','tx:c','SPENT_BY'),('e3','out:h:0','tx:i','SPENT_BY')")
        graph.storage_relative_path = "group-fixture.duckdb"
        start = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
        findings = []
        for n, (rule, focal, edges, day, locator) in enumerate([
            ("peeling_chain", "addr:a", ["e1"], 0, "/0"),
            ("peeling_chain", "addr:b", ["e2"], 0, "/1"),
            ("peeling_chain", "addr:h", ["e3"], 0, "/2"),
            ("peeling_chain", "addr:a", ["e1"], 1, "/0"),
            ("fan_out", "addr:hub", [], 0, "/0"),
            ("fan_out", "addr:hub", [], 0, "/0"),
            ("fan_out", "addr:hub", [], 0, "/2"),
        ]):
            f = FindingRecord(id=f"group-fixture-{n}", case_id=case, snapshot_id=job.snapshot_id,
                graph_snapshot_id=graph.id, entity_ref=focal, window_start=start+timedelta(days=day,minutes=n),
                window_end=start+timedelta(days=day,minutes=n+1), rule_id=rule, rule_version="fixture-v1",
                claim="Observed structural episode", raw_score=.5, feature_vector_hash=str(n),
                feature_vector={"detector_result":{"graph_path":{"edge_ids":edges}}},
                source_refs=[{"evidence_id":job.source_id,"locator":locator}], status="open")
            session.add(f)
            findings.append(f.id)
        session.commit()
        result = investigations.materialize(session, case_id=case, snapshot_id=job.snapshot_id, graph=graph, evidence_root=runner.settings.evidence_root)
        session.commit()
    return client, sessions, case, headers, graph.id, result, findings


def test_episode_connections_repetition_boundaries_and_hubs(system, monkeypatch):
    _, sessions, _, _, _, result, ids = fixture_groups(system, monkeypatch)
    with sessions() as session:
        member = {fid: session.scalar(select(InvestigationMember.group_id).join(InvestigationGroup)
                  .where(InvestigationMember.finding_id==fid, InvestigationGroup.run_id==result["run_id"])) for fid in ids}
        assert member[ids[0]] == member[ids[1]]
        assert member[ids[0]] != member[ids[2]] != member[ids[3]]
        assert member[ids[4]] == member[ids[5]] != member[ids[6]]
        assert session.get(InvestigationGroup, member[ids[0]]).transaction_count == 3
        assert sum(session.scalars(select(InvestigationGroup.member_count).where(InvestigationGroup.run_id==result["run_id"]))) == result["underlying_findings"]


def test_queue_counts_capacity_ties_pagination_and_independent_reviews(system, monkeypatch):
    client, sessions, case, headers, _, result, _ids = fixture_groups(system, monkeypatch)
    endpoint=f"/v1/cases/{case}/investigation-queue"
    for k in (0,1,100):
        response = client.get(endpoint,headers=headers,params={"capacity":k}).json()
        assert response["queued_groups"] == min(k, response["unresolved_groups"])
        assert response["backlog_groups"] == max(0,response["unresolved_groups"]-k)
        assert response["investigation_groups"] == result["groups"]
        assert response["underlying_findings"] == result["underlying_findings"]
    first=client.get(endpoint,headers=headers,params={"limit":1}).json()
    second=client.get(endpoint,headers=headers,params={"limit":1,"offset":1}).json()
    assert first["items"][0]["group_id"] != second["items"][0]["group_id"]
    gid=first["items"][0]["group_id"]
    assert client.post(f"/v1/investigation-groups/{gid}/reviews",headers=headers,json={"expected_review_version":1,"disposition":"triaged","reason":"   "}).status_code==422
    item=client.get(f"/v1/investigation-groups/{gid}",headers=headers)
    assert item.status_code==200,item.text
    individual=client.get(f"/v1/investigation-groups/{gid}/members",headers=headers).json()["items"][0]["finding"]
    response=client.post(f"/v1/findings/{individual['finding_id']}/reviews",headers=headers,json={"expected_finding_version":1,"disposition":"dismissed","reason":"Individual structural observation is benign in the provided record"})
    assert response.status_code==201,response.text
    response=client.post(f"/v1/investigation-groups/{gid}/reviews",headers=headers,json={"expected_review_version":1,"disposition":"confirmed","reason":"Confirmed only the bounded observed episode proposition"})
    assert response.status_code==201,response.text
    assert client.post(f"/v1/investigation-groups/{gid}/reviews",headers=headers,json={"expected_review_version":1,"disposition":"confirmed","reason":"Stale"}).status_code==409
    after=client.get(endpoint,headers=headers).json()
    assert after["unresolved_groups"]==first["unresolved_groups"]-1
    with sessions() as session:
        assert session.get(FindingRecord,individual["finding_id"]).status=="dismissed"
        assert session.scalar(select(func.count()).select_from(ReviewDecisionRecord))==1
        assert session.scalar(select(func.count()).select_from(InvestigationReview))==1
    for suffix in ("", "/members", "/reviews", "/export", "/subjects", "/replacements"):
        assert client.get(f"/v1/investigation-groups/{gid}{suffix}",headers={"X-TraceX-Actor":"outsider"}).status_code==404
    assert client.post(f"/v1/investigation-groups/{gid}/reviews",headers={"X-TraceX-Actor":"outsider"},json={"expected_review_version":2,"disposition":"open","reason":"Unauthorized"}).status_code==404
    assert client.get(endpoint,headers={"X-TraceX-Actor":"outsider"}).status_code==404


def test_retries_preserve_reviews_and_recompute_replaces_membership(system, monkeypatch):
    client,sessions,case,headers,graph_id,result,ids=fixture_groups(system,monkeypatch)
    gid=client.get(f"/v1/cases/{case}/investigation-queue",headers=headers).json()["items"][0]["group_id"]
    assert client.post(f"/v1/investigation-groups/{gid}/reviews",headers=headers,json={"expected_review_version":1,"disposition":"triaged","reason":"Preserve reviewed membership"}).status_code==201
    with sessions() as session:
        graph=session.get(GraphSnapshot,graph_id)
        old=list(session.scalars(select(InvestigationMember.finding_id).where(InvestigationMember.group_id==gid)))
        again=investigations.materialize(session,case_id=case,snapshot_id=graph.snapshot_id,graph=graph,evidence_root=runner.settings.evidence_root)
        assert again["reused"] and again["run_id"]==result["run_id"]
        # New evidence procedure output -> new immutable generation, old reviews
        # not copied or silently broadened.
        session.get(FindingRecord,ids[0]).feature_vector_hash="changed-detector-output"
        session.flush()
        changed=investigations.materialize(session,case_id=case,snapshot_id=graph.snapshot_id,graph=graph,evidence_root=runner.settings.evidence_root)
        session.commit()
        assert changed["run_id"]!=result["run_id"]
        assert session.get(InvestigationGroup,gid).status=="triaged"
        assert list(session.scalars(select(InvestigationMember.finding_id).where(InvestigationMember.group_id==gid)))==old
        assert session.scalar(select(func.count()).select_from(InvestigationReplacement))>0
        assert all(s=="open" for s in session.scalars(select(InvestigationGroup.status).where(InvestigationGroup.run_id==changed["run_id"])))


def test_group_opposing_citations_require_real_case_receipts_and_pin_replay(system, monkeypatch):
    client, _, case, headers, _, _, _ = fixture_groups(system, monkeypatch)
    gid = client.get(f"/v1/cases/{case}/investigation-queue", headers=headers).json()["items"][0]["group_id"]
    ref = client.get(f"/v1/investigation-groups/{gid}/members", headers=headers).json()["items"][0]["finding"]["source_refs"][0]
    endpoint = f"/v1/investigation-groups/{gid}/reviews"
    body = {"expected_review_version": 1, "disposition": "triaged", "reason": "Cite only the actual observed record; no automatic opposition or innocence verdict"}
    for stale in ({**ref, "source_sha256": "stale"}, {**ref, "locator": "/10000"}, {**ref, "evidence_id": "other-case-or-missing-source"}):
        assert client.post(endpoint, headers=headers, json={**body, "counterevidence_refs": [stale]}).status_code == 422
    saved = client.post(endpoint, headers=headers, json={**body, "counterevidence_refs": [ref]})
    assert saved.status_code == 201, saved.text
    cited = client.get(endpoint, headers=headers).json()["items"][0]["counterevidence_refs"][0]
    assert cited["source_sha256"]
    replay = client.get(f"/v1/evidence/{cited['evidence_id']}/records", headers=headers, params={"locator": cited["locator"]})
    assert replay.status_code == 200, replay.text
    assert replay.json()["source_sha256"] == cited["source_sha256"]


def test_failed_materialization_is_atomic_and_required_stage_retries(system,monkeypatch):
    client,sessions=system
    original=investigations.observations
    def broken(*args):
        raise RuntimeError("Injected grouping failure")
    monkeypatch.setattr(investigations,"observations",broken)
    case,job_id,headers=upload(client)
    assert runner.process_one("group-fail")
    job=client.get(f"/v1/jobs/{job_id}",headers=headers).json()
    assert job["state"]=="failed"
    assert any(s["name"]=="investigation_grouping" and s["status"]=="failed" for s in job["analysis"]["stages"])
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(InvestigationRun))==0
    monkeypatch.setattr(investigations,"observations",original)
    assert client.post(f"/v1/cases/{case}/analysis/retry?job_id={job_id}",headers=headers).status_code==202
    assert runner.process_one("group-retry")
    job=client.get(f"/v1/jobs/{job_id}",headers=headers).json()
    assert job["state"]=="completed"
    assert any(s["name"]=="investigation_grouping" and s["status"]=="complete" for s in job["analysis"]["stages"])


def test_queue_coverage_does_not_treat_legacy_or_unpublished_groups_as_empty_review(system, monkeypatch):
    client, sessions, case, headers, _, result, _ = fixture_groups(system, monkeypatch)
    endpoint = f"/v1/cases/{case}/investigation-queue"
    complete = client.get(endpoint, headers=headers).json()
    assert complete["grouping_coverage"]["state"] == "complete"
    assert complete["grouping_coverage"]["grouped_findings"] == complete["underlying_findings"]
    assert complete["grouping_coverage"]["ungrouped_findings"] == 0
    with sessions() as session:
        run = session.get(InvestigationRun, result["run_id"])
        run.state = "building"
        session.commit()
    incomplete = client.get(endpoint, headers=headers).json()
    assert incomplete["queued_groups"] == 0
    assert incomplete["grouping_coverage"]["state"] == "incomplete"
    assert incomplete["grouping_coverage"]["ungrouped_findings"] == complete["underlying_findings"]
    assert "authorized analysis retry" in incomplete["grouping_coverage"]["reason"]
    with sessions() as session:
        run = session.get(InvestigationRun, result["run_id"])
        run.state = "complete"
        run.active = False
        session.commit()
    legacy = client.get(endpoint, headers=headers).json()
    assert legacy["grouping_coverage"]["ungrouped_findings"] == complete["underlying_findings"]
    with sessions() as session:
        session.get(InvestigationRun, result["run_id"]).active = True
        session.commit()
    restored = client.get(endpoint, headers=headers).json()
    assert restored["grouping_coverage"] == complete["grouping_coverage"]
    assert restored["investigation_groups"] == complete["investigation_groups"]
    assert client.get(endpoint, headers={"X-TraceX-Actor": "outsider"}).status_code == 404
