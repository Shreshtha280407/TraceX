"""Small parity/complexity checks; no reserved labels or large transaction inputs."""
import copy
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from app.engine import grouping_materializer as fast
from app.engine import investigations as reference
from app.jobs.grouping import GroupingProgress
from app.jobs.service import job_progress
from app.models import (
    FindingParticipation,
    FindingRecord,
    GraphSnapshot,
    ImportJob,
    InvestigationAnchor,
    InvestigationGroup,
    InvestigationMember,
    InvestigationReplacement,
    InvestigationRun,
    InvestigationSubject,
)
from tests.unit.test_analysis_acceptance import system as analysis_system
from tests.unit.test_investigations import fixture_groups
from workers import runner


@pytest.fixture
def system(tmp_path, monkeypatch):
    yield from analysis_system.__wrapped__(tmp_path, monkeypatch)


def projection(session, run_id):
    groups = select(InvestigationGroup.id).where(InvestigationGroup.run_id == run_id)
    result = {}
    for model in (InvestigationGroup, InvestigationAnchor, InvestigationMember,
                  InvestigationSubject, InvestigationReplacement):
        column = model.replacement_id if model == InvestigationReplacement else (
            model.id if model == InvestigationGroup else model.group_id)
        result[model.__tablename__] = sorted([
            tuple(row) for row in session.execute(select(model.__table__).where(column.in_(groups)))
        ], key=repr)
    return result


def new_generation(sessions, snapshot_id, count):
    with sessions() as session:
        original = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == snapshot_id))
        fields = {column.name: copy.deepcopy(getattr(original, column.name))
                  for column in FindingRecord.__table__.columns
                  if column.name not in {"id", "created_at", "updated_at"}}
        start = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
        for n in range(count):
            # >256 repeated observations exercise caps and page-boundary matching;
            # empty anchors and cross-day/reversed windows remain singletons.
            beginning = start + timedelta(seconds=n)
            ending = beginning + timedelta(seconds=1)
            if n % 71 == 0:
                ending = start + timedelta(days=1)
            if n % 73 == 0:
                ending = beginning - timedelta(seconds=1)
            refs = [] if n % 79 == 0 else [{"evidence_id": "fixture-evidence", "locator": "/0", "source_sha256": "fixture-hash"}]
            f = FindingRecord(**{**fields, "id": reference.identifier(f"parity-{snapshot_id}-{n}"),
                "rule_id": "fan_out", "rule_version": "parity-v1", "entity_ref": "addr:bounded-focal",
                "window_start": beginning, "window_end": ending, "source_refs": refs,
                "feature_vector": {}, "feature_vector_hash": f"parity-{n}"})
            session.add(f)
            session.flush()
            session.add(FindingParticipation(finding_id=f.id, transaction_ref=f"tx:actual-{n % 17}"))
        session.commit()


def test_disk_index_has_exact_reference_memberships_ids_counts_and_replacements(system, monkeypatch, record_property):
    client, sessions, case, headers, graph_id, initial, _ids = fixture_groups(system, monkeypatch)
    gid = client.get(f"/v1/cases/{case}/investigation-queue", headers=headers).json()["items"][0]["group_id"]
    assert client.post(f"/v1/investigation-groups/{gid}/reviews", headers=headers, json={
        "expected_review_version": 1, "disposition": "triaged", "reason": "Keep prior reviewed membership"}).status_code == 201
    new_generation(sessions, client.get(f"/v1/investigation-groups/{gid}", headers=headers).json()["snapshot_id"], 600)
    with sessions() as session:
        graph = session.get(GraphSnapshot, graph_id)
        before = projection(session, initial["run_id"])
        savepoint = session.begin_nested()
        started = time.monotonic()
        baseline = reference.materialize(session, case_id=case, snapshot_id=graph.snapshot_id,
                                         graph=graph, evidence_root=runner.settings.evidence_root)
        baseline_seconds = time.monotonic() - started
        expected = projection(session, baseline["run_id"])
        savepoint.rollback()
        progress = []
        monkeypatch.setattr(fast, "PAGE", 64)
        started = time.monotonic()
        actual = fast.materialize(session, case_id=case, snapshot_id=graph.snapshot_id, graph=graph,
                                  evidence_root=runner.settings.evidence_root, progress=progress.append)
        fast_seconds = time.monotonic() - started
        session.commit()
        assert actual["run_id"] == baseline["run_id"]
        assert actual["procedure_sha256"] == baseline["procedure_sha256"]
        assert actual["groups"] == baseline["groups"]
        assert actual["underlying_findings"] == baseline["underlying_findings"]
        assert projection(session, actual["run_id"]) == expected
        assert projection(session, initial["run_id"]) == before
        assert session.get(InvestigationGroup, gid).status == "triaged"
        assert max(session.scalars(select(InvestigationGroup.member_count))) == 256
        prepared = [p["prepared_findings"] for p in progress if p["phase"] == "building"]
        assert prepared == sorted(prepared) and prepared[-1] == actual["underlying_findings"]
        assert not list((runner.settings.evidence_root / ".grouping-scratch").glob("grouping-*"))
        again = fast.materialize(session, case_id=case, snapshot_id=graph.snapshot_id,
                                  graph=graph, evidence_root=runner.settings.evidence_root)
        assert again["reused"] and projection(session, actual["run_id"]) == expected
    record_property("reference_seconds_607_findings_3_transactions", baseline_seconds)
    record_property("disk_index_seconds_607_findings_3_transactions", fast_seconds)


def test_database_selects_scale_with_pages_not_each_finding(system, monkeypatch):
    _client, sessions, case, _headers, graph_id, initial, _ids = fixture_groups(system, monkeypatch)
    with sessions() as session:
        snapshot_id = session.get(GraphSnapshot, graph_id).snapshot_id
    new_generation(sessions, snapshot_id, 300)
    statements = []
    with sessions() as session:
        graph = session.get(GraphSnapshot, graph_id)
        engine = session.get_bind()
        def counted(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        event.listen(engine, "before_cursor_execute", counted)
        try:
            actual = fast.materialize(session, case_id=case, snapshot_id=snapshot_id,
                                      graph=graph, evidence_root=runner.settings.evidence_root)
            session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", counted)
        # Snapshot/digest/prior + findings + two prefetches per page, not 5+ per row.
        assert len(statements) < 20, len(statements)
        assert actual["underlying_findings"] == 307 and actual["run_id"] != initial["run_id"]


def test_failed_bulk_publication_leaves_prior_generation_and_reviews_unchanged(system, monkeypatch):
    _client, sessions, case, _headers, graph_id, initial, _ids = fixture_groups(system, monkeypatch)
    with sessions() as session:
        snapshot_id = session.get(GraphSnapshot, graph_id).snapshot_id
    new_generation(sessions, snapshot_id, 20)
    original = fast.bulk_insert_serialized
    def fail_after_groups(session, model, rows, json_columns):
        if model == InvestigationMember:
            raise RuntimeError("fixture: publication failed after group rows")
        original(session, model, rows, json_columns)
    monkeypatch.setattr(fast, "bulk_insert_serialized", fail_after_groups)
    with sessions() as session:
        graph = session.get(GraphSnapshot, graph_id)
        before = projection(session, initial["run_id"])
        with pytest.raises(RuntimeError, match="publication failed"):
            fast.materialize(session, case_id=case, snapshot_id=snapshot_id, graph=graph,
                             evidence_root=runner.settings.evidence_root)
        assert projection(session, initial["run_id"]) == before
        assert session.get(InvestigationRun, initial["run_id"]).active
        assert session.scalar(select(func.count()).select_from(InvestigationRun)) == 2  # original empty run + fixture
        assert not list((runner.settings.evidence_root / ".grouping-scratch").glob("grouping-*"))


def test_grouping_progress_never_claims_completion_and_missing_denominator_is_explicit():
    job = ImportJob(state="running", stage="investigation_grouping", rows_seen=10, rows_accepted=10)
    assert not job_progress(job)["determinate"]
    for phase, done in (("building", 0), ("building", 42), ("publishing", 100)):
        actual = job_progress(job, {"stages": [{"name": "investigation_grouping", "details": {
            "grouping_progress": {"phase": phase, "prepared_findings": done,
                                  "total_findings": 100, "prepared_groups": 11}}}]})
        assert 99.5 <= actual["percent"] < 100 and actual["determinate"]
        assert f"{done} of 100" in actual["basis"] and "publish together" in actual["basis"]


def test_progress_side_channel_fails_closed_and_sqlite_does_not_spawn_writer(system):
    _client, sessions = system
    with sessions() as session:
        job = ImportJob(id="fixture", attempt=1, lease_owner="worker")
        with GroupingProgress(session, job, 10) as progress:
            assert not progress.enabled and progress.thread is None
            progress({"phase": "building", "prepared_findings": 5})
            assert progress.details["prepared_findings"] == 5
            progress.error = RuntimeError("fixture lease lost")
            with pytest.raises(RuntimeError, match="lease lost"):
                progress({})
            progress.error = None


def test_progress_renews_repeatedly_and_stops_its_thread(system, monkeypatch):
    _client, sessions = system
    with sessions() as session:
        progress = GroupingProgress(session, ImportJob(id="fixture", attempt=1, lease_owner="worker"), 1)
        progress.enabled = True  # Test the clock/thread, without a second SQLite writer.
        progress.interval = .01
        calls = []
        renewed = threading.Event()
        def persist():
            calls.append(dict(progress.details))
            if len(calls) >= 2:
                renewed.set()
        monkeypatch.setattr(progress, "_persist", persist)
        with progress:
            progress({"prepared_findings": 42, "total_findings": 100})
            assert renewed.wait(2), "a long stage must renew, not just lease once at entry"
        assert len(calls) >= 2 and calls[-1]["prepared_findings"] == 42
        assert not progress.thread.is_alive()


def test_failed_lease_renewal_propagates_and_cannot_publish_success(system, monkeypatch):
    _client, sessions = system
    with sessions() as session:
        progress = GroupingProgress(session, ImportJob(id="fixture", attempt=1, lease_owner="worker"), 1)
        progress.enabled = True
        progress.interval = .01
        failed = threading.Event()
        calls = []
        def persist():
            calls.append(1)
            if len(calls) > 1:
                failed.set()
                raise RuntimeError("fixture: lease renewal failed")
        monkeypatch.setattr(progress, "_persist", persist)
        with pytest.raises(RuntimeError, match="lease renewal failed"), progress:
            assert failed.wait(2)
        assert not progress.thread.is_alive()
