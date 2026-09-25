"""Gap-Closure follow-up: `entity_graph_sync` composes WP-2's real Postgres
entity/candidate records with `project_entity`/`project_entity_resolution_
candidate` -- the wiring step that was missing entirely (see
`docs/qa/known-limitations.md`'s "WP-2 has no Neo4j footprint" entry).

Mirrors `test_entity_projection.py`/`test_projection.py`'s `_FakeRepository`
pattern for the Neo4j side, and `FakeEntityRepository` (already used by
`test_entity_api.py`/`test_entity_service.py`) for the Postgres side --
no live Neo4j or PostgreSQL needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.modules.graph.entity_graph_sync import (
    project_case_candidates,
    project_case_entities,
    sync_case_entities_and_candidates,
)
from app.modules.graph.entity_models import EntityResolutionCandidateRecord, EntityReviewOutcome
from tests.fixtures.factories import make_entity
from tests.fixtures.graph.fake_entity_repository import FakeEntityRepository


class _FakeGraphRepository:
    """Mirrors `test_entity_projection.py`'s `_FakeRepository` exactly, plus
    records which entity/candidate IDs were actually written, so ordering
    (entities before candidates) can be asserted directly."""

    def __init__(self) -> None:
        self.write_calls: list[tuple[str, dict[str, Any]]] = []
        self.projected_entity_ids: set[str] = set()

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.write_calls.append((query, parameters))
        if "MERGE (n:Entity" in query:
            self.projected_entity_ids.add(parameters["entity_id"])
            return [{"entity_id": parameters["entity_id"]}]
        if "POSSIBLY_SAME_AS" in query:
            if (
                parameters["left_entity_id"] in self.projected_entity_ids
                and parameters["right_entity_id"] in self.projected_entity_ids
            ):
                return [{"candidate_id": parameters["candidate_id"]}]
            return []
        if "CONTRADICTED_BY" in query:
            return [{"candidate_id": parameters["candidate_id"]}]
        raise AssertionError(f"unexpected query: {query}")

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("this sync module never reads from the graph")


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


async def test_project_case_entities_merges_every_entity() -> None:
    graph = _FakeGraphRepository()
    entities = [make_entity(), make_entity(), make_entity()]

    count = await project_case_entities(graph, entities)

    assert count == 3
    assert len(graph.write_calls) == 3
    assert all("MERGE (n:Entity" in query for query, _ in graph.write_calls)


async def test_project_case_candidates_uses_needs_review_when_no_decision_exists() -> None:
    graph = _FakeGraphRepository()
    entity_repository = FakeEntityRepository()
    case_id = uuid4()
    left = make_entity(case_id=case_id)
    right = make_entity(case_id=case_id)
    graph.projected_entity_ids = {str(left.entity_id), str(right.entity_id)}
    candidate = _candidate(
        case_id=case_id, left_entity_id=left.entity_id, right_entity_id=right.entity_id
    )

    applied, deferred = await project_case_candidates(
        graph, entity_repository, case_id, [candidate]
    )

    assert (applied, deferred) == (1, 0)
    query, params = graph.write_calls[0]
    assert "POSSIBLY_SAME_AS" in query
    assert params["effective_status"] == "needs_review"


async def test_project_case_candidates_reflects_the_latest_real_decision() -> None:
    """The Neo4j edge's status must match `GET /entity-candidates`'s own
    `entity_resolution_review_view` computation -- never a stale value."""
    graph = _FakeGraphRepository()
    entity_repository = FakeEntityRepository()
    case_id = uuid4()
    left = make_entity(case_id=case_id)
    right = make_entity(case_id=case_id)
    graph.projected_entity_ids = {str(left.entity_id), str(right.entity_id)}
    candidate = _candidate(
        case_id=case_id, left_entity_id=left.entity_id, right_entity_id=right.entity_id
    )
    await entity_repository.record_decision(
        case_id=case_id,
        entity_resolution_candidate_id=candidate.entity_resolution_candidate_id,
        decision=EntityReviewOutcome.VERIFIED_SAME,
        reviewer_user_id=uuid4(),
        rationale=None,
        decision_id=uuid4(),
    )

    applied, deferred = await project_case_candidates(
        graph, entity_repository, case_id, [candidate]
    )

    assert (applied, deferred) == (1, 0)
    _query, params = graph.write_calls[0]
    assert params["effective_status"] == "verified_same"


async def test_project_case_candidates_defers_when_an_entity_is_missing() -> None:
    graph = _FakeGraphRepository()
    entity_repository = FakeEntityRepository()
    case_id = uuid4()
    candidate = _candidate(case_id=case_id)  # neither entity ever projected

    applied, deferred = await project_case_candidates(
        graph, entity_repository, case_id, [candidate]
    )

    assert (applied, deferred) == (0, 1)


async def test_sync_projects_entities_before_candidates_so_nothing_defers() -> None:
    """The real ordering bug this module exists to avoid: projecting
    candidates before their entities would defer every single one."""
    graph = _FakeGraphRepository()
    entity_repository = FakeEntityRepository()
    case_id = uuid4()
    left = make_entity(case_id=case_id)
    right = make_entity(case_id=case_id)
    candidate = _candidate(
        case_id=case_id, left_entity_id=left.entity_id, right_entity_id=right.entity_id
    )

    summary = await sync_case_entities_and_candidates(
        graph, entity_repository, case_id, [left, right], [candidate]
    )

    assert summary.projected_entity_count == 2
    assert summary.applied_candidate_count == 1
    assert summary.deferred_candidate_count == 0


async def test_sync_is_idempotent_on_a_second_pass() -> None:
    """Re-running against the same real data (e.g. a repeated
    `--resolve-entities` invocation) must never error or duplicate."""
    graph = _FakeGraphRepository()
    entity_repository = FakeEntityRepository()
    case_id = uuid4()
    left = make_entity(case_id=case_id)
    right = make_entity(case_id=case_id)
    candidate = _candidate(
        case_id=case_id, left_entity_id=left.entity_id, right_entity_id=right.entity_id
    )

    first = await sync_case_entities_and_candidates(
        graph, entity_repository, case_id, [left, right], [candidate]
    )
    second = await sync_case_entities_and_candidates(
        graph, entity_repository, case_id, [left, right], [candidate]
    )

    assert first.applied_candidate_count == second.applied_candidate_count == 1
