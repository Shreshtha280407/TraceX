"""The anomaly stack over the real ingestion → graph → findings path.

The offline harness reads generated NDJSON directly, which is right for a
benchmark and wrong as a definition of the product. This test proves the same
stack runs on a *committed, receipt-approved snapshot* and writes findings the
existing API already serves — so the ranking is part of TraceX, not beside it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base, make_engine
from app.jobs.service import create_or_reuse_job
from app.ml.facts import facts_from_records, load_facts_from_snapshot
from app.ml.findings import ML_RULE_VERSION, review_decision_labels
from app.models import (
    Case,
    CaseEvent,
    CaseMembership,
    EvidenceSource,
    FindingRecord,
    GraphSnapshot,
    ImportJob,
    ReviewDecisionRecord,
    Snapshot,
    User,
)
from workers import runner

REPO = Path(__file__).resolve().parents[2]


def _rows() -> list[dict]:
    """A small, deterministic source with a real equal-output shape and peel chain.

    Hand-built rather than sampled from the 100K fixture so the test runs in
    seconds and its expected structure is readable.
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


@pytest.fixture(scope="module")
def committed_snapshot(tmp_path_factory: pytest.TempPathFactory):
    """Ingest through the real pipeline and return a completed snapshot."""
    root = tmp_path_factory.mktemp("ml-pipeline")
    evidence_root = root / "evidence"
    database_url = f"sqlite:///{root / 'control.db'}"

    source_path = root / "rows.ndjson"
    source_path.write_text("\n".join(json.dumps(item) for item in _rows()) + "\n", encoding="utf-8")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as session:
        user = User(external_subject="ml-pipeline-test")
        session.add(user)
        session.flush()
        case = Case(name="ML pipeline integration", synthetic=True, created_by=user.id)
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
            session, case_id=case.id, source=source, idempotency_key="ml-pipeline", actor=user
        )
        session.commit()
        case_id, job_id, user_id = case.id, job.id, user.id

    settings = Settings(
        database_url=database_url, evidence_root=evidence_root,
        max_upload_bytes=source_path.stat().st_size + 1, lease_seconds=600,
        event_heartbeat_seconds=1,
    )
    # Drive the real worker in-process, the same way the Phase 5B scripts do.
    old_settings, old_sessions = runner.settings, runner.SessionLocal
    runner.settings, runner.SessionLocal = settings, sessions
    try:
        assert runner.process_one("ml-pipeline-worker"), "worker did not claim the ingestion job"
    finally:
        runner.settings, runner.SessionLocal = old_settings, old_sessions

    with sessions() as session:
        job = session.get(ImportJob, job_id)
        assert job is not None and job.state == "completed", (
            f"ingestion did not complete: {job.state if job else None}"
        )
    return sessions, evidence_root, case_id, job_id, user_id


def test_facts_load_from_the_committed_snapshot(committed_snapshot) -> None:
    """The stack reads receipt-approved fragments, not files off disk."""
    sessions, evidence_root, _, job_id, _ = committed_snapshot
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

    sessions, evidence_root, _, job_id, _ = committed_snapshot
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
    sessions, _, case_id, job_id, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        stored = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id,
                FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))
        event = session.scalars(
            select(CaseEvent)
            .where(CaseEvent.case_id == case_id, CaseEvent.event_type == "import.completed")
            .order_by(CaseEvent.sequence.desc())
        ).first()

    assert stored, "ingestion completed without producing any anomaly-stack findings"
    assert event is not None
    # The outcome is on the completion event, so a skip is visible rather than silent.
    assert event.payload["ml_status"] == "written"
    assert event.payload["ml_finding_count"] == len(stored)
    # Deterministic Phase 4 findings are untouched and still reported separately.
    assert "finding_count" in event.payload


def test_ml_findings_are_ranked_evidenced_and_hedged(committed_snapshot) -> None:
    """Whatever ingestion wrote has to be reviewable, not just ordered."""
    sessions, _, _case_id, job_id, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        stored = list(session.scalars(
            select(FindingRecord).where(
                FindingRecord.snapshot_id == job.snapshot_id,
                FindingRecord.rule_version == ML_RULE_VERSION,
            )
        ))

    assert [item.rank for item in sorted(stored, key=lambda x: x.rank)] == list(range(1, len(stored) + 1))
    for item in stored:
        assert item.entity_ref.startswith("transaction:")
        assert item.source_refs, "a finding with no source locator cannot be reopened"
        assert item.benign_alternatives, "a finding must carry benign explanations"
        assert any(entry["kind"] == "coverage_limitation" for entry in item.opposing_evidence)
        assert item.coverage["window_boundary_incomplete"] is True
        assert item.status == "open"
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


def test_a_failing_stack_cannot_fail_the_import(committed_snapshot, monkeypatch) -> None:
    """Evidence and deterministic findings must survive a ranking that blows up.

    An import that has already committed correct facts must not be rolled back
    because an optional model could not produce a score.
    """
    from app.engine.ingestion import pipeline

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("app.ml.findings.materialize_ml_findings", explode)
    sessions, evidence_root, _case_id, job_id, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        snapshot = session.get(Snapshot, job.snapshot_id)
        graph = session.scalars(
            select(GraphSnapshot).where(GraphSnapshot.snapshot_id == snapshot.id)
        ).first()
        settings = Settings(
            database_url="sqlite://", evidence_root=evidence_root, max_upload_bytes=1,
            lease_seconds=1, event_heartbeat_seconds=1,
        )
        outcome = pipeline._materialize_ml_findings(
            session, settings=settings, snapshot=snapshot, graph=graph
        )
    assert outcome["written"] == 0
    assert outcome["status"] == "error:RuntimeError"


def test_the_stack_can_be_turned_off(committed_snapshot) -> None:
    """A deployment that does not want the ranking must be able to say so."""
    from app.engine.ingestion import pipeline

    sessions, evidence_root, _case_id, job_id, _ = committed_snapshot
    with sessions() as session:
        job = session.get(ImportJob, job_id)
        snapshot = session.get(Snapshot, job.snapshot_id)
        graph = session.scalars(
            select(GraphSnapshot).where(GraphSnapshot.snapshot_id == snapshot.id)
        ).first()
        settings = Settings(
            database_url="sqlite://", evidence_root=evidence_root, max_upload_bytes=1,
            lease_seconds=1, event_heartbeat_seconds=1, ml_findings_enabled=False,
        )
        outcome = pipeline._materialize_ml_findings(
            session, settings=settings, snapshot=snapshot, graph=graph
        )
    assert outcome == {"written": 0, "status": "disabled"}


def test_supervised_labels_require_enough_review_decisions(committed_snapshot) -> None:
    """Layer S must refuse to train until analysts have actually reviewed enough.

    Returning `None` here is the whole point: it forces the deployed ranking to
    stay unsupervised until a non-circular label source exists.
    """
    sessions, evidence_root, case_id, job_id, user_id = committed_snapshot
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
