"""Gap-Closure WP-3 (G10): `POSSIBLY_SAME_AS`/`CONTRADICTED_BY` projection.

Mirrors `test_projection.py`'s `_FakeRepository` pattern exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.modules.graph.entity_models import EntityResolutionCandidateRecord
from app.modules.graph.entity_projection import project_entity_resolution_candidate
from app.modules.graph.models import ProjectionOutcome


class _FakeRepository:
    def __init__(self, write_results: list[list[dict[str, Any]]] | None = None) -> None:
        self._write_results = list(write_results or [])
        self.write_calls: list[tuple[str, dict[str, Any]]] = []

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.write_calls.append((query, parameters))
        return self._write_results.pop(0) if self._write_results else []

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("projection never reads")


def _candidate(**overrides: Any) -> EntityResolutionCandidateRecord:
    data: dict[str, Any] = {
        "entity_resolution_candidate_id": uuid4(),
        "case_id": uuid4(),
        "left_entity_id": uuid4(),
        "right_entity_id": uuid4(),
        "reasons": ("exact_identifier",),
        "identifier_types": ("phone",),
        "vector_score": None,
        "contradiction_reasons": (),
        "supporting_observation_ids": (uuid4(),),
        "config_version": "entity_resolution_cascade_v1",
        "created_at": datetime.now(UTC),
    }
    data.update(overrides)
    return EntityResolutionCandidateRecord(**data)


async def test_applies_when_both_entities_exist() -> None:
    candidate = _candidate()
    repository = _FakeRepository(write_results=[[{"candidate_id": "x"}]])

    outcome = await project_entity_resolution_candidate(
        repository, candidate, effective_status="needs_review"
    )

    assert outcome is ProjectionOutcome.APPLIED
    query, params = repository.write_calls[0]
    assert "POSSIBLY_SAME_AS" in query
    assert str(candidate.left_entity_id) not in query
    assert params["left_entity_id"] == str(candidate.left_entity_id)
    assert params["right_entity_id"] == str(candidate.right_entity_id)
    assert params["effective_status"] == "needs_review"


async def test_defers_when_an_entity_is_missing() -> None:
    """`MATCH` (not `MERGE`) on both entities means no rows returned when either is absent."""
    candidate = _candidate()
    repository = _FakeRepository(write_results=[[]])

    outcome = await project_entity_resolution_candidate(
        repository, candidate, effective_status="needs_review"
    )

    assert outcome is ProjectionOutcome.DEFERRED
    assert len(repository.write_calls) == 1, "must not attempt CONTRADICTED_BY after a deferral"


async def test_contradiction_reasons_project_a_second_edge() -> None:
    candidate = _candidate(contradiction_reasons=("conflicting_event_window",))
    repository = _FakeRepository(write_results=[[{"candidate_id": "x"}], [{"candidate_id": "x"}]])

    outcome = await project_entity_resolution_candidate(
        repository, candidate, effective_status="needs_review"
    )

    assert outcome is ProjectionOutcome.APPLIED
    assert len(repository.write_calls) == 2
    second_query, second_params = repository.write_calls[1]
    assert "CONTRADICTED_BY" in second_query
    assert second_params["contradiction_reasons"] == ["conflicting_event_window"]


async def test_no_contradiction_reasons_means_only_one_write() -> None:
    candidate = _candidate(contradiction_reasons=())
    repository = _FakeRepository(write_results=[[{"candidate_id": "x"}]])

    await project_entity_resolution_candidate(
        repository, candidate, effective_status="needs_review"
    )

    assert len(repository.write_calls) == 1


async def test_query_is_parameterized_never_interpolated() -> None:
    candidate = _candidate()
    repository = _FakeRepository(write_results=[[{"candidate_id": "x"}]])

    await project_entity_resolution_candidate(
        repository, candidate, effective_status="verified_same"
    )

    query, params = repository.write_calls[0]
    assert str(candidate.case_id) not in query
    assert str(candidate.entity_resolution_candidate_id) not in query
    assert "$case_id" in query
    assert "$candidate_id" in query
