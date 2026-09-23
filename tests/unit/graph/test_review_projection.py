"""Phase 6 Part 5: safe Neo4j projection tests with no live graph.

Mirrors `test_intelligence_projection.py`'s fake-repository pattern exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.graph.hypothesis_models import HypothesisRecord, HypothesisStatus
from app.modules.graph.review_models import CandidateReviewDecisionRecord, CandidateReviewOutcome
from app.modules.graph.review_projection import (
    project_candidate_review_decision,
    project_hypothesis,
    project_hypothesis_candidate_reference,
    project_hypothesis_review_decision,
)

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


class _Graph:
    def __init__(self, *, return_records: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._return_records = return_records

    async def write(self, query: str, parameters: dict[str, object]) -> list[dict[str, object]]:
        self.calls.append((query, parameters))
        return [{"ok": True}] if self._return_records else []


def _decision(case_id, candidate_link_id, *, rationale: str | None = "sensitive narrative"):
    return CandidateReviewDecisionRecord(
        candidate_review_decision_id=uuid4(),
        case_id=case_id,
        candidate_link_id=candidate_link_id,
        correlation_id=uuid4(),
        decision=CandidateReviewOutcome.ACCEPTED_BY_REVIEWER,
        reviewer_user_id=uuid4(),
        rationale=rationale,
        rationale_commitment_sha256=None if rationale is None else "a" * 64,
        created_at=_NOW,
    )


def _hypothesis(case_id, hypothesis_id) -> HypothesisRecord:
    return HypothesisRecord(
        hypothesis_id=hypothesis_id,
        case_id=case_id,
        status=HypothesisStatus.NEEDS_REVIEW,
        created_by=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        decided_at=None,
        decided_by=None,
        supporting_observation_ids=(uuid4(),),
        supporting_candidate_ids=(),
        supporting_entity_resolution_candidate_ids=(),
        statement="the raw human-authored statement text",
        statement_commitment_sha256="b" * 64,
        rationale="the raw human-authored rationale text",
        rationale_commitment_sha256="c" * 64,
    )


async def test_candidate_review_projection_uses_match_not_merge_for_correlation() -> None:
    graph = _Graph()
    case_id, candidate_link_id = uuid4(), uuid4()
    decision = _decision(case_id, candidate_link_id)
    applied = await project_candidate_review_decision(
        graph,  # type: ignore[arg-type]
        case_id=case_id,
        projection_key="synthetic-projection-key",
        decision=decision,
    )
    assert applied is True
    query, parameters = graph.calls[0]
    assert "MATCH (c:Correlation" in query
    assert "MERGE" not in query
    assert "sensitive narrative" not in str(parameters)
    assert parameters["properties"]["review_status"] == "accepted_by_reviewer"


async def test_candidate_review_projection_is_a_safe_no_op_when_correlation_is_absent() -> None:
    graph = _Graph(return_records=False)
    case_id, candidate_link_id = uuid4(), uuid4()
    decision = _decision(case_id, candidate_link_id)
    applied = await project_candidate_review_decision(
        graph,  # type: ignore[arg-type]
        case_id=case_id,
        projection_key="missing-projection-key",
        decision=decision,
    )
    assert applied is False


async def test_hypothesis_projection_is_provenance_gated_like_correlation() -> None:
    graph = _Graph()
    case_id, hypothesis_id = uuid4(), uuid4()
    hypothesis = _hypothesis(case_id, hypothesis_id)
    evidence_id, observation_id = uuid4(), uuid4()
    applied = await project_hypothesis(
        graph,  # type: ignore[arg-type]
        hypothesis=hypothesis,
        evidence_paths=((evidence_id, observation_id),),
    )
    assert applied is True
    query, parameters = graph.calls[0]
    assert "YIELDED_OBSERVATION" in query
    assert "WHERE found = size($evidence_paths)" in query
    assert "the raw human-authored statement text" not in str(parameters)
    assert "the raw human-authored rationale text" not in str(parameters)
    assert parameters["properties"]["kind"] == "hypothesis"
    assert (
        parameters["properties"]["statement_commitment_sha256"]
        == hypothesis.statement_commitment_sha256
    )


async def test_hypothesis_projection_is_a_safe_no_op_with_no_evidence_paths() -> None:
    graph = _Graph()
    hypothesis = _hypothesis(uuid4(), uuid4())
    applied = await project_hypothesis(graph, hypothesis=hypothesis, evidence_paths=())  # type: ignore[arg-type]
    assert applied is False
    assert graph.calls == []


async def test_hypothesis_candidate_reference_uses_match_both_sides() -> None:
    graph = _Graph()
    case_id, hypothesis_id = uuid4(), uuid4()
    applied = await project_hypothesis_candidate_reference(
        graph,  # type: ignore[arg-type]
        case_id=case_id,
        hypothesis_id=hypothesis_id,
        projection_key="synthetic-projection-key",
    )
    assert applied is True
    query, _ = graph.calls[0]
    assert query.count("MATCH") == 2
    assert "MERGE (h)-[:REFERENCES_CANDIDATE]->(c)" in query


async def test_hypothesis_review_projection_never_carries_raw_rationale() -> None:
    graph = _Graph()
    case_id, hypothesis_id = uuid4(), uuid4()
    applied = await project_hypothesis_review_decision(
        graph,  # type: ignore[arg-type]
        case_id=case_id,
        hypothesis_id=hypothesis_id,
        status="accepted_by_reviewer",
        decided_at=_NOW,
        decided_by=uuid4(),
        rationale_commitment_sha256="d" * 64,
    )
    assert applied is True
    query, parameters = graph.calls[0]
    assert "MATCH (h:Hypothesis" in query
    assert "MERGE" not in query
    assert parameters["properties"]["status"] == "accepted_by_reviewer"
    assert parameters["properties"]["reviewer_rationale_commitment_sha256"] == "d" * 64
