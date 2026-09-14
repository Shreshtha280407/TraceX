"""Synthetic-only tests for Phase 5 reviewable intelligence behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.graph.intelligence.analytics import (
    analyse,
    detect_communication_transfer_movement_motifs,
)
from app.modules.graph.intelligence.correlation import (
    add_temporal_hot_window_reason,
    build_correlation_submission,
)
from app.modules.graph.intelligence.models import (
    GraphEdgeSnapshot,
    ObservationDescriptor,
    RetrievalReason,
)
from app.modules.graph.intelligence.retrieval import retrieve_candidates
from app.modules.graph.intelligence.scoring import score_candidates
from app.modules.graph.intelligence.vector_store import PgvectorCandidateStore, source_snapshot_hash


def _item(case_id, *, phone: str, alias: str, at: int = 0) -> ObservationDescriptor:
    observation_id = uuid4()
    return ObservationDescriptor(
        case_id=case_id,
        observation_id=observation_id,
        evidence_id=uuid4(),
        source_locator_reference=f"synthetic:{observation_id}",
        identifiers={"phone": phone},
        aliases=(alias,),
        transliterations=("raama",) if alias != "Rama" else (),
        event_start=datetime(2026, 1, 1, 12, at, tzinfo=UTC),
        event_end=datetime(2026, 1, 1, 12, at + 1, tzinfo=UTC),
    )


def test_exact_identifier_blocking_is_deterministic_case_scoped_and_candidate_only() -> None:
    case_id = uuid4()
    first, second = (
        _item(case_id, phone="+919999000001", alias="राम"),
        _item(case_id, phone="+919999000001", alias="Rama"),
    )
    candidates = retrieve_candidates([first, second])
    assert candidates == retrieve_candidates([second, first])
    assert candidates[0].reasons[0] is RetrievalReason.EXACT_IDENTIFIER
    scored = score_candidates(candidates)
    assert scored[0].status.value in {"candidate", "needs_review"}
    assert "never identity verification" in scored[0].explanation


def test_cross_case_retrieval_is_rejected() -> None:
    with pytest.raises(ValueError, match="multiple cases"):
        retrieve_candidates(
            [
                _item(uuid4(), phone="+919999000001", alias="A"),
                _item(uuid4(), phone="+919999000001", alias="A"),
            ]
        )


def test_local_vector_snapshot_is_deterministic_and_case_bound() -> None:
    case_id = uuid4()
    item = _item(case_id, phone="+919999000009", alias="निशा")
    assert source_snapshot_hash(item) == source_snapshot_hash(item)
    changed_case = item.model_copy(update={"case_id": uuid4()})
    assert source_snapshot_hash(item) != source_snapshot_hash(changed_case)


class _VectorResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self._rows


class _VectorConnection:
    def __init__(self) -> None:
        self.statement = None
        self.parameters = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return _VectorResult([])


class _VectorEngine:
    def __init__(self) -> None:
        self.connection = _VectorConnection()

    def connect(self):
        return self.connection


async def test_pgvector_search_binds_the_descriptor_case_before_returning_candidates() -> None:
    descriptor = _item(uuid4(), phone="+919999000009", alias="Nisha")
    engine = _VectorEngine()

    results = await PgvectorCandidateStore(engine).search(descriptor)  # type: ignore[arg-type]

    assert results == []
    assert "WHERE case_id = :case_id" in str(engine.connection.statement)
    assert engine.connection.parameters["case_id"] == descriptor.case_id
    assert engine.connection.parameters["observation_id"] == descriptor.observation_id


def test_conflicting_source_backed_identifier_is_retained_as_a_contradiction() -> None:
    case_id = uuid4()
    first = _item(case_id, phone="+919999000007", alias="Same Alias")
    second = _item(case_id, phone="+919999000008", alias="Same Alias")
    candidate = retrieve_candidates([first, second])[0]
    assert candidate.contradiction_reasons == ("conflicting_phone_claim",)
    score = score_candidates((candidate,))[0]
    assert any(item.feature == "contradiction" for item in score.contributions)


def test_submission_uses_nipun_candidate_only_seam_with_reproducible_snapshot() -> None:
    case_id = uuid4()
    scored = score_candidates(
        retrieve_candidates(
            [
                _item(case_id, phone="+919999000003", alias="राम"),
                _item(case_id, phone="+919999000003", alias="Rama"),
            ]
        )
    )
    submission = build_correlation_submission(scored)
    assert submission.status.value == "needs_review"
    assert submission.feature_snapshot is not None
    assert submission.feature_snapshot.values["candidate_only"] is True


def test_temporal_reason_requires_bounded_same_case_windows() -> None:
    case_id = uuid4()
    first, second = (
        _item(case_id, phone="+919999000002", alias="A", at=0),
        _item(case_id, phone="+919999000002", alias="B", at=2),
    )
    retrieved = retrieve_candidates([first, second])
    temporal = add_temporal_hot_window_reason(retrieved, (first, second))
    assert RetrievalReason.TEMPORAL in temporal[0].reasons
    no_time = first.model_copy(update={"event_start": None, "event_end": None})
    assert (
        RetrievalReason.TEMPORAL
        not in add_temporal_hot_window_reason(retrieved, (no_time, second))[0].reasons
    )


def test_analytics_are_case_scoped_and_deterministic_for_a_snapshot() -> None:
    case_id = uuid4()
    first_id, second_id, third_id = uuid4(), uuid4(), uuid4()
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    edges = (
        GraphEdgeSnapshot(
            case_id=case_id,
            left_id=first_id,
            right_id=second_id,
            event_id=uuid4(),
            evidence_observation_ids=(uuid4(),),
            event_kind="cdr_call",
            event_start=base,
            event_end=base.replace(minute=1),
        ),
        GraphEdgeSnapshot(
            case_id=case_id,
            left_id=second_id,
            right_id=third_id,
            event_id=uuid4(),
            evidence_observation_ids=(uuid4(),),
            event_kind="financial_transaction",
            event_start=base.replace(minute=2),
            event_end=base.replace(minute=3),
        ),
        GraphEdgeSnapshot(
            case_id=case_id,
            left_id=third_id,
            right_id=uuid4(),
            event_id=uuid4(),
            evidence_observation_ids=(uuid4(),),
            event_kind="movement",
            event_start=base.replace(minute=4),
            event_end=base.replace(minute=5),
        ),
    )
    first, second = analyse(edges), analyse(edges)
    assert first[0].graph_snapshot_hash == second[0].graph_snapshot_hash
    assert first[0].values == second[0].values
    assert any(key.startswith("leiden:") for key in first[0].values)
    assert any(key.startswith("betweenness:") for key in first[0].values)
    motif = detect_communication_transfer_movement_motifs(edges)
    assert motif and motif[0].supporting_observation_ids
