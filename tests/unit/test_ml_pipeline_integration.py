"""The anomaly stack over the real ingestion → graph → findings path.

The offline harness reads generated NDJSON directly, which is right for a
benchmark and wrong as a definition of the product. This test proves the same
stack runs on a *committed, receipt-approved snapshot* and writes findings the
existing API already serves — so the ranking is part of TraceX, not beside it.

Every test that claims to prove "automatic integration" drives the real worker
entry point (`workers.runner.process_one`) or the real ingestion function
(`app.engine.ingestion.pipeline.ingest_source`) — never
`app.ml.findings.materialize_ml_findings` called directly. A test that only
calls that function and asserts on its return value proves the function works;
it does not prove ingestion calls it on its own, which is the actual Phase 5C
requirement.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.jobs.service import create_or_reuse_job
from app.ml.facts import facts_from_records, load_facts_from_snapshot
from app.ml.findings import (
    ML_RULE_VERSION,
    RELEASE_ID,
    model_run_id,
    release_identity,
    release_manifest_sha256,
    review_decision_labels,
)
from app.models import (
    Case,
    CaseEvent,
    CaseMembership,
    EvidenceSource,
    FindingRecord,
    ImportJob,
    ReviewDecisionRecord,
    User,
)
from workers import runner

REPO = Path(__file__).resolve().parents[2]


def _rows() -> list[dict]:
    """A small, deterministic source with a real equal-output shape and peel chain.

    Hand-built rather than sampled from the 100K fixture so each ingestion in this
    file runs in a fraction of a second.
    """
    rows: list[dict] = []
    base = "2026-05-01T00:%02d:00Z"

    def row(index: int, inputs: list[dict], outputs: list[dict], minute: int) -> dict:
        return {
            "source_record_id": f"r-{index:04d}",
            "timestamp": base % minute,
            "network": "bitcoin-regtest",
            "txid": hashlib.sha256(f"itest:{index}".encode()).hexdigest(),
            "fee": "0.00000500",
            "script_type": "p2wpkh",
            "src_ip": f"198.51.100.{1 + index % 7}",
            "dst_ip": "203.0.113.5",
            "src_port": 8333,
            "dst_port": 8333,
            "geo_country": ("US", "DE", "SG")[index % 3],
            "asn": f"AS{64512 + index % 5}",
            # Funding rows carry no prior amount; an unknown value stays absent
            # rather than being invented as zero.
            "input_addresses": [item["address"] for item in inputs if item["address"] is not None],
            "input_amounts": [
                f"{item['amount_sats'] / 1e8:.8f}" for item in inputs if item["amount_sats"] is not None
            ],
            "output_addresses": [item["address"] for item in outputs],
            "output_amounts": [f"{item['amount_sats'] / 1e8:.8f}" for item in outputs],
            "inputs": inputs,
            "outputs": outputs,
        }

    def address(tag: str) -> str:
        return f"bcrt1q{hashlib.sha256(tag.encode()).hexdigest()[:38]}"

    # 60 funding transactions with unresolved prevouts: the starting ledger.
    for index in range(60):
        rows.append(row(
            index,
            [{"prev_txid": None, "prev_vout": None, "address": None, "amount_sats": None, "sequence": 0}],
            [{"address": address(f"fund:{index}"), "amount_sats": 3_000_000, "script_type": "p2wpkh"}],
            index % 55,
        ))
    # An equal-output shape spending three of them.
    funders = [hashlib.sha256(f"itest:{index}".encode()).hexdigest() for index in range(3)]
    rows.append(row(
        60,
        [{"prev_txid": txid, "prev_vout": 0, "address": address(f"fund:{index}"),
          "amount_sats": 3_000_000, "sequence": 0} for index, txid in enumerate(funders)],
        [{"address": address(f"mix:{slot}"), "amount_sats": 2_500_000, "script_type": "p2wpkh"} for slot in range(3)]
        + [{"address": address("mix:change"), "amount_sats": 1_499_500, "script_type": "p2wpkh"}],
        56,
    ))
    # A two-hop peel chain off funder 10.
    peel_source = hashlib.sha256(b"itest:10").hexdigest()
    rows.append(row(
        61,
        [{"prev_txid": peel_source, "prev_vout": 0, "address": address("fund:10"),
          "amount_sats": 3_000_000, "sequence": 0}],
        [{"address": address("peel:0"), "amount_sats": 2_899_500, "script_type": "p2wpkh"},
         {"address": address("peel:small0"), "amount_sats": 100_000, "script_type": "p2wpkh"}],
        57,
    ))
    rows.append(row(
        62,
        [{"prev_txid": hashlib.sha256(b"itest:61").hexdigest(), "prev_vout": 0,
          "address": address("peel:0"), "amount_sats": 2_899_500, "sequence": 0}],
        [{"address": address("peel:1"), "amount_sats": 2_799_000, "script_type": "p2wpkh"},
         {"address": address("peel:small1"), "amount_sats": 100_000, "script_type": "p2wpkh"}],
        58,
    ))
    return rows


def _ingest_fresh(tmp_path_factory: pytest.TempPathFactory, *, label: str, settings_overrides: dict | None = None):
    """Build an isolated case + source + job and drive it through the real worker.

    Every scenario test (ML disabled, missing dependency, invalid budget, a
    raised exception) calls this with different `settings_overrides` so each one
    exercises `workers.runner.process_one` -- the actual automatic entry point --
    rather than reaching into the pipeline's internals directly.

    Returns `(sessions, evidence_root, case_id, job_id, user_id, settings)`.
    """
    root = tmp_path_factory.mktemp(label)
    evidence_root = root / "evidence"
    database_url = f"sqlite:///{root / 'control.db'}"

    source_path = root / "rows.ndjson"
    source_path.write_text("\n".join(json.dumps(item) for item in _rows()) + "\n", encoding="utf-8")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as session:
        user = User(external_subject=f"{label}-user")
        session.add(user)
        session.flush()
        case = Case(name=f"ML pipeline integration: {label}", synthetic=True, created_by=user.id)
        session.add(case)
        session.flush()
        session.add(CaseMembership(case_id=case.id, user_id=user.id, role="case_lead"))
        relative = Path(case.id) / source_hash / "original"
        (evidence_root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, evidence_root / relative)
        source = EvidenceSource(
            case_id=case.id, sha256=source_hash, byte_size=source_path.stat().st_size,
            original_filename="rows.ndjson", source_format="ndjson",
            storage_relative_path=relative.as_posix(), synthetic=True,
        )
        session.add(source)
        session.flush()
        job, _ = create_or_reuse_job(
            session, case_id=case.id, source=source, idempotency_key=label, actor=user
        )
        session.commit()
        case_id, job_id, user_id = case.id, job.id, user.id

    settings = Settings(
        database_url=database_url, evidence_root=evidence_root,
        max_upload_bytes=source_path.stat().st_size + 1, lease_seconds=600,
        event_heartbeat_seconds=1, **(settings_overrides or {}),
    )
    old_settings, old_sessions = runner.settings, runner.SessionLocal
    runner.settings, runner.SessionLocal = settings, sessions
    try:
        assert runner.process_one(f"{label}-worker"), "worker did not claim the ingestion job"
    finally:
        runner.settings, runner.SessionLocal = old_settings, old_sessions

    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job is not None and job.state == "completed", (
            f"ingestion did not complete: {job.state if job else None}"
        )
    return sessions, evidence_root, case_id, job_id, user_id, settings


def _completion_event(sessions, case_id: str) -> CaseEvent:
    with sessions() as session:
        event = session.scalars(
            select(CaseEvent)
            .where(CaseEvent.case_id == case_id, CaseEvent.event_type == "import.completed")
            .order_by(CaseEvent.sequence.desc())
        ).first()
    assert event is not None, "ingestion never recorded an import.completed event"
    return event


@pytest.fixture(scope="module")
def committed_snapshot(tmp_path_factory: pytest.TempPathFactory):
    """The default-settings case: ML enabled, everything expected to succeed."""
    return _ingest_fresh(tmp_path_factory, label="ml-pipeline-default")


def test_facts_load_from_the_committed_snapshot(committed_snapshot) -> None:
    """The stack reads receipt-approved fragments, not files off disk."""
    sessions, evidence_root, _, job_id, _, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        facts = load_facts_from_snapshot(session, evidence_root, job.snapshot_id)

    assert facts.transaction_count == 63
    # Ordering is by committed block time, not by fragment read order.
    assert (facts.tx_time[1:] >= facts.tx_time[:-1]).all()
    # Five spend edges resolve inside the snapshot: three inputs of the
    # equal-output transaction, plus one per peel step.
    assert int((facts.in_prev >= 0).sum()) == 5
    assert int((facts.out_spent_by >= 0).sum()) == 5


def test_the_stack_detects_the_shapes_through_the_real_path(committed_snapshot) -> None:
    """Grain A must see the equal-output shape and the peel steps in snapshot facts."""
    from app.engine.graph.builder import _facts
    from app.ml import grains

    sessions, evidence_root, _, job_id, _, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        facts = facts_from_records(_facts(session, evidence_root, job.snapshot_id))

    table = grains.build_transaction_table(facts)
    columns = {name: index for index, name in enumerate(table.columns)}
    equal = table.matrix[:, columns["equal_group_exact"]]
    peel = table.matrix[:, columns["one_big_one_small"]]
    assert int((equal >= 3).sum()) == 1, "the equal-output transaction was not recovered from the snapshot"
    assert int(peel.sum()) == 2, "both peel steps should be recovered from the snapshot"


def test_ingestion_writes_ml_findings_without_being_asked(committed_snapshot) -> None:
    """The trigger: a completed import must produce the ranking on its own.

    Nothing in this test calls the stack. If the ingestion pipeline does not run it
    at completion, the ranking exists only in a benchmark script and never reaches
    the findings endpoint or the frontend.
    """
    sessions, _, case_id, job_id, _, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        stored = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id,
                FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
    event = _completion_event(sessions, case_id)

    assert stored, "ingestion completed without producing any anomaly-stack findings"
    # The outcome is on the completion event, so a skip is visible rather than silent.
    assert event.payload["ml_status"] == "written"
    assert event.payload["ml_finding_count"] == len(stored)
    assert event.payload["ml_release_id"] == RELEASE_ID
    assert event.payload["ml_model_run_id"]
    # Deterministic Phase 4 findings are untouched and still reported separately.
    assert "finding_count" in event.payload


def test_ml_findings_are_ranked_evidenced_and_hedged(committed_snapshot) -> None:
    """Whatever ingestion wrote has to be reviewable, not just ordered.

    This also proves requirement 9: every stored ML finding carries a release ID,
    a model_run_id, and a release_manifest_sha256 that a reviewer -- or a test --
    can recompute independently from `app.ml.findings.release_identity()`.
    """
    sessions, _, _case_id, job_id, _, settings = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        stored = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id,
                FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))

    assert stored
    expected_run_id = model_run_id(snapshot_id=stored[0].snapshot_id, budget=settings.ml_review_budget)
    assert [item.rank for item in sorted(stored, key=lambda x: x.rank)] == list(range(1, len(stored) + 1))
    for item in stored:
        # "tx:", matching the graph builder's own transaction-node ID scheme
        # (app/engine/graph/builder.py) -- not "transaction:", which the graph
        # never recognizes as a seed and silently resolves to zero nodes.
        assert item.entity_ref.startswith("tx:")
        assert item.source_refs, "a finding with no source locator cannot be reopened"
        assert item.benign_alternatives, "a finding must carry benign explanations"
        assert any(entry["kind"] == "coverage_limitation" for entry in item.opposing_evidence)
        assert item.coverage["window_boundary_incomplete"] is True
        assert item.status == "open"
        # Release identity: recomputable, not just stamped.
        assert item.coverage["release_id"] == RELEASE_ID
        assert item.coverage["model_run_id"] == expected_run_id
        assert item.coverage["release_manifest_sha256"] == release_manifest_sha256()
        assert item.feature_vector["release_id"] == RELEASE_ID
        assert item.feature_vector["model_run_id"] == expected_run_id
        # A triage rank, never a verdict.
        claim = item.claim.lower()
        assert "triage" in claim or "prioritize" in claim
        assert "criminal" not in claim and "illicit" not in claim
        # No supervised model may contribute to what a case actually stores.
        assert any("No supervised model" in note for note in item.coverage["notes"])
        # The hard-excluded Phase 4.1 fields never appear in a stored vector.
        text = json.dumps(item.feature_vector)
        for forbidden in ("risk_propagation_score", "risk_seed_distance", "risk_seed_count"):
            assert forbidden not in text


def test_release_identity_is_the_frozen_phase5b_decision() -> None:
    """A_global + D_burst, 1% budget, stouffer fusion -- the decision must not drift."""
    identity = release_identity()
    assert identity["release_id"] == RELEASE_ID == "anomaly-stack-v1"
    assert tuple(identity["layers"]) == ("A_global", "D_burst")
    assert identity["fusion_method"] == "stouffer"
    assert identity["default_review_budget"] == 0.01
    # Deterministic and recomputable: same inputs, same ID, every time.
    first = model_run_id(snapshot_id="snap-x", budget=0.01)
    second = model_run_id(snapshot_id="snap-x", budget=0.01)
    assert first == second
    assert first != model_run_id(snapshot_id="snap-y", budget=0.01)
    assert first != model_run_id(snapshot_id="snap-x", budget=0.02)


def test_ml_findings_are_idempotent_across_a_repeated_ingest_call(committed_snapshot) -> None:
    """Simulates a worker restart: the same completed job is re-processed.

    `app.engine.ingestion.pipeline.ingest_source` is the real entry point a
    restarted worker resumes into for a job whose lease expired mid-run, or that
    an operator re-triggers. Calling it a second time on an already-completed
    job must not duplicate the graph, the deterministic findings, or the ML
    findings -- this exercises the production idempotency guard, not a
    hand-rolled one.
    """
    from app.engine.ingestion.pipeline import ingest_source

    sessions, _, _case_id, job_id, _, settings = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        before = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
        assert before

        source = session.get(EvidenceSource, job.source_id)
        ingest_source(session, settings=settings, job=job, source=source)
        session.commit()

        after = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
    assert len(after) == len(before), "re-processing the same completed job duplicated ML findings"
    assert {item.id for item in after} == {item.id for item in before}


def test_ml_findings_visible_through_the_real_findings_api(committed_snapshot) -> None:
    """GET /v1/cases/{case_id}/findings and the evidence endpoint, over HTTP.

    Proves requirement 3 literally: the ranking is visible through the *existing*
    case-scoped findings API, using the real FastAPI app and a real request, not
    a direct database read standing in for one.
    """
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app

    sessions, _, case_id, _job_id, _, _ = committed_snapshot

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        headers = {"X-TraceX-Actor": "ml-pipeline-default-user"}
        response = client.get(f"/v1/cases/{case_id}/findings?limit=200", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["ml_enabled"] is True, "the list endpoint did not detect the ML finding it just returned"
        assert ML_RULE_VERSION in body["methods"]

        ml_findings = [item for item in body["findings"] if item["rule_version"] == ML_RULE_VERSION]
        assert ml_findings, "no ML finding reached the findings API"
        top = ml_findings[0]
        assert top["coverage"]["release_id"] == RELEASE_ID
        assert top["coverage"]["model_run_id"]

        evidence = client.get(f"/v1/findings/{top['finding_id']}/evidence", headers=headers)
        assert evidence.status_code == 200
        evidence_body = evidence.json()
        # "Source evidence can be opened": the locator list a reviewer would
        # follow back to the exact raw record is non-empty and round-trips.
        assert evidence_body["source_refs"], "an ML finding's evidence has no source locator to open"
        assert evidence_body["feature_vector"]["release_id"] == RELEASE_ID
        assert evidence_body["coverage"]["model_run_id"] == top["coverage"]["model_run_id"]
        # Phase 7: the replay contract must say a model produced this row, not the
        # deterministic-only default -- an evidence bundle that claims "no model
        # ran" for a row an ML release actually scored is exactly the kind of
        # unreviewable-provenance failure the export contract exists to prevent.
        assert evidence_body["replay_contract"]["ml_enabled"] is True
        assert evidence_body["replay_contract"]["release_id"] == RELEASE_ID
        assert evidence_body["replay_contract"]["model_run_id"] == top["coverage"]["model_run_id"]

        deterministic = [item for item in body["findings"] if item["rule_version"] != ML_RULE_VERSION]
        assert deterministic, "fixture should also carry a deterministic finding for this to be a real mix"
        det_evidence = client.get(f"/v1/findings/{deterministic[0]['finding_id']}/evidence", headers=headers).json()
        assert det_evidence["replay_contract"]["ml_enabled"] is False
        assert det_evidence["replay_contract"]["release_id"] is None

        export = client.get(f"/v1/cases/{case_id}/findings/export", headers=headers)
        assert export.status_code == 200
        export_body = export.json()
        assert export_body["ml_enabled"] is True, "export must reflect the ML finding it actually contains"
        assert export_body["method"] == "mixed"
        assert set(export_body["methods"]) >= {ML_RULE_VERSION, deterministic[0]["rule_version"]}
    finally:
        app.dependency_overrides.clear()


def test_a_failing_stack_cannot_fail_the_import(tmp_path_factory, monkeypatch) -> None:
    """A ranking that raises must not take the import down with it.

    Drives the *real* worker entry point with `app.ml.findings.materialize_ml_findings`
    monkeypatched to explode, rather than calling the pipeline's internal helper
    directly -- so this proves the exception is caught on the actual automatic
    path, not merely that the helper's own try/except works in isolation.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("app.ml.findings.materialize_ml_findings", explode)
    sessions, _, case_id, job_id, _, _ = _ingest_fresh(tmp_path_factory, label="ml-pipeline-raises")

    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job.state == "completed", "an ML scoring failure must not fail the import"
        deterministic = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == "deterministic-v1",
            )
        ))
        ml_rows = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
    assert deterministic, "deterministic Phase 4 findings must survive an ML scoring failure"
    assert not ml_rows, "no ML finding should be stored when scoring raised"

    event = _completion_event(sessions, case_id)
    assert event.payload["ml_status"] == "error:RuntimeError"
    assert event.payload["ml_finding_count"] == 0


def test_the_stack_can_be_turned_off(tmp_path_factory) -> None:
    """TRACEX_ML_FINDINGS=0 must skip ML while everything else proceeds normally,
    driven through the real worker with that setting, not the internal helper."""
    sessions, _, case_id, job_id, _, _ = _ingest_fresh(
        tmp_path_factory, label="ml-pipeline-disabled", settings_overrides={"ml_findings_enabled": False},
    )
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job.state == "completed"
        deterministic = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == "deterministic-v1",
            )
        ))
        ml_rows = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
    assert deterministic, "disabling ML must not disable deterministic findings"
    assert not ml_rows

    event = _completion_event(sessions, case_id)
    assert event.payload["ml_status"] == "disabled"
    assert event.payload["ml_finding_count"] == 0
    assert "finding_count" in event.payload  # Phase 4 count still reported


def test_missing_ml_dependency_degrades_through_real_ingestion(tmp_path_factory, monkeypatch) -> None:
    """The optional `ml` extra not being installed must degrade to `unavailable`,
    through the real ingestion path -- not by calling the helper directly.

    `sys.modules["app.ml.findings"] = None` is the standard way to simulate "this
    import fails" for a module that a prior import in this same test session has
    already cached: CPython treats a `None` entry as "this import previously
    failed" and raises ImportError immediately, exactly mirroring what happens
    when numpy/scikit-learn/scipy are genuinely not installed. monkeypatch
    restores the real module afterward, so no other test is affected.
    """
    monkeypatch.setitem(sys.modules, "app.ml.findings", None)
    sessions, _, case_id, job_id, _, _ = _ingest_fresh(tmp_path_factory, label="ml-pipeline-unavailable")

    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job.state == "completed"
        deterministic = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == "deterministic-v1",
            )
        ))
    assert deterministic, "a missing optional dependency must not affect deterministic findings"

    event = _completion_event(sessions, case_id)
    assert event.payload["ml_status"] == "unavailable"
    assert event.payload["ml_finding_count"] == 0


@pytest.mark.parametrize("budget", [0.0, -0.1, 0.75, 5.0, float("nan"), float("inf")])
def test_unsafe_review_budgets_fail_safely_and_visibly(tmp_path_factory, budget: float) -> None:
    """A budget outside (0, 0.5] must skip ML with a distinct, visible status --
    never silently score against a nonsensical threshold, never raise."""
    sessions, _, case_id, job_id, _, _ = _ingest_fresh(
        tmp_path_factory, label=f"ml-pipeline-badbudget-{abs(hash(budget))}",
        settings_overrides={"ml_review_budget": budget},
    )
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job.state == "completed", f"an unsafe budget ({budget}) must not fail the import"
        ml_rows = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id, FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
    assert not ml_rows

    event = _completion_event(sessions, case_id)
    assert event.payload["ml_status"] == "invalid_budget"


def test_non_numeric_review_budget_falls_back_without_crashing_startup() -> None:
    """A garbage env value for TRACEX_ML_REVIEW_BUDGET must not take the app down.

    This is the narrower failure `Settings._parse_ml_review_budget` guards, kept
    distinct from the range check above: a value that fails at `float()` entirely
    would otherwise raise out of `Settings.from_environment()` at process start.
    """
    assert Settings._parse_ml_review_budget("not-a-number") == 0.01
    assert Settings._parse_ml_review_budget("") == 0.01
    assert Settings._parse_ml_review_budget("0.02") == 0.02


def test_supervised_labels_require_enough_review_decisions(committed_snapshot) -> None:
    """Layer S must refuse to train until analysts have actually reviewed enough.

    Returning `None` here is the whole point: it forces the deployed ranking to
    stay unsupervised until a non-circular label source exists.
    """
    sessions, evidence_root, case_id, job_id, user_id, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        facts = load_facts_from_snapshot(session, evidence_root, job.snapshot_id)
        assert review_decision_labels(session, case_id=case_id, facts=facts) is None

        finding = session.scalars(
            select(FindingRecord).where(FindingRecord.case_id == case_id).limit(1)
        ).first()
        assert finding is not None
        session.add(ReviewDecisionRecord(
            case_id=case_id, finding_id=finding.id, finding_version=1,
            actor_id=user_id, disposition="confirmed", reason="integration test",
        ))
        session.commit()
        # One decision is still nowhere near enough to fit on.
        assert review_decision_labels(session, case_id=case_id, facts=facts) is None


def test_makefile_dataset_target_uses_the_uv_managed_python() -> None:
    """`make dataset` must never depend on whatever `python3` happens to resolve
    to on PATH -- on many macOS installs that is a pre-3.11 system interpreter,
    below this generator's floor, and fails with a confusing error instead of a
    clear version message."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    lines = makefile.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "dataset:")
    recipe: list[str] = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    assert recipe, "the dataset: target has no recipe"
    assert any(step.startswith("uv run") for step in recipe), (
        "dataset: must invoke the project-managed interpreter via `uv run`"
    )
    assert not any(
        step.startswith(("python3 ", "python ")) or step in ("python3", "python")
        for step in recipe
    ), "dataset: must not call a bare python3/python directly"
