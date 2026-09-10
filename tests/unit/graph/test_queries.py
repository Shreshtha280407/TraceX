"""Unit tests for `app/modules/graph/queries.py`.

No real Neo4j: a minimal in-memory double stands in for `Neo4jGraphRepository`
(see `tests/integration/graph/` for tests against a live database).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.modules.graph.errors import GraphNotFoundError, GraphValidationError
from app.modules.graph.queries import (
    DEFAULT_PAGE_SIZE,
    MAX_COLLECTION_LIMIT,
    MAX_PAGE_SIZE,
    get_case_graph_summary,
    get_entity_events,
    get_event_with_participants,
    get_evidence_provenance,
    get_observation_provenance,
)


class _UnusedRepository:
    """Fails the test if a query helper ever reaches the repository.

    Used for cases that must be rejected by argument validation alone.
    """

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("repository.write should not be called")

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("repository.read should not be called")


class _FakeRepository:
    """Returns canned read results in call order; records every call made."""

    def __init__(self, read_results: list[list[dict[str, Any]]] | None = None) -> None:
        self._read_results = list(read_results or [])
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("queries.py should never write")

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.read_calls.append((query, parameters))
        return self._read_results.pop(0) if self._read_results else []


# --- Requirement 8: missing/empty case_id is rejected -----------------------


@pytest.mark.parametrize("bad_case_id", [None, ""])
async def test_get_case_graph_summary_rejects_bad_case_id(bad_case_id: Any) -> None:
    with pytest.raises(GraphValidationError):
        await get_case_graph_summary(_UnusedRepository(), bad_case_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_case_id", [None, ""])
async def test_get_evidence_provenance_rejects_bad_case_id(bad_case_id: Any) -> None:
    with pytest.raises(GraphValidationError):
        await get_evidence_provenance(_UnusedRepository(), bad_case_id, uuid4())  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_case_id", [None, ""])
async def test_get_observation_provenance_rejects_bad_case_id(bad_case_id: Any) -> None:
    with pytest.raises(GraphValidationError):
        await get_observation_provenance(_UnusedRepository(), bad_case_id, uuid4())  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_case_id", [None, ""])
async def test_get_event_with_participants_rejects_bad_case_id(bad_case_id: Any) -> None:
    with pytest.raises(GraphValidationError):
        await get_event_with_participants(_UnusedRepository(), bad_case_id, uuid4())  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_case_id", [None, ""])
async def test_get_entity_events_rejects_bad_case_id(bad_case_id: Any) -> None:
    with pytest.raises(GraphValidationError):
        await get_entity_events(_UnusedRepository(), bad_case_id, uuid4())  # type: ignore[arg-type]


# --- Requirement 9: every public query method has a bounded limit policy ---


def test_pagination_constants_are_sane_and_bounded() -> None:
    assert 0 < DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE < 10_000
    assert 0 < MAX_COLLECTION_LIMIT < 10_000


async def test_get_entity_events_rejects_non_positive_limit() -> None:
    with pytest.raises(GraphValidationError):
        await get_entity_events(_UnusedRepository(), uuid4(), uuid4(), limit=0)


async def test_get_entity_events_rejects_limit_above_max() -> None:
    with pytest.raises(GraphValidationError):
        await get_entity_events(_UnusedRepository(), uuid4(), uuid4(), limit=MAX_PAGE_SIZE + 1)


async def test_get_entity_events_rejects_negative_offset() -> None:
    with pytest.raises(GraphValidationError):
        await get_entity_events(_UnusedRepository(), uuid4(), uuid4(), offset=-1)


async def test_get_entity_events_reports_has_more_beyond_the_page() -> None:
    case_id, entity_id = uuid4(), uuid4()
    event_rows = [{"event": _event_props(uuid4(), case_id)} for _ in range(3)]
    repository = _FakeRepository(
        read_results=[
            [{"entity_id": str(entity_id)}],  # existence check
            event_rows,  # fetch_limit = limit + 1 = 3 rows come back
        ]
    )
    page = await get_entity_events(repository, case_id, entity_id, limit=2)
    assert len(page.events) == 2
    assert page.has_more is True
    assert page.limit == 2
    # The fetch itself is bounded to limit + 1, never unbounded.
    _, second_call_params = repository.read_calls[1]
    assert second_call_params["fetch_limit"] == 3


async def test_get_entity_events_not_found_raises() -> None:
    repository = _FakeRepository(read_results=[[]])  # existence check finds nothing
    with pytest.raises(GraphNotFoundError):
        await get_entity_events(repository, uuid4(), uuid4())


async def test_evidence_provenance_query_bounds_collected_observations() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    repository = _FakeRepository(
        read_results=[[{"evidence": _evidence_props(evidence_id, case_id), "page": [], "total": 0}]]
    )
    await get_evidence_provenance(repository, case_id, evidence_id)
    query, params = repository.read_calls[0]
    assert "[0..$limit]" in query
    assert params["limit"] == MAX_COLLECTION_LIMIT


async def test_event_with_participants_query_bounds_collected_participants() -> None:
    case_id, event_id = uuid4(), uuid4()
    repository = _FakeRepository(
        read_results=[[{"event": _event_props(event_id, case_id), "page": [], "total": 0}]]
    )
    await get_event_with_participants(repository, case_id, event_id)
    query, params = repository.read_calls[0]
    assert "[0..$limit]" in query
    assert params["limit"] == MAX_COLLECTION_LIMIT


# --- Case isolation: every MATCH filters by case_id, not only the anchor ---


async def test_evidence_provenance_filters_case_id_on_traversal_target_too() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    repository = _FakeRepository(
        read_results=[[{"evidence": _evidence_props(evidence_id, case_id), "page": [], "total": 0}]]
    )
    await get_evidence_provenance(repository, case_id, evidence_id)
    query, _ = repository.read_calls[0]
    assert "Observation {case_id: $case_id}" in query


async def test_event_with_participants_filters_case_id_on_traversal_target_too() -> None:
    case_id, event_id = uuid4(), uuid4()
    repository = _FakeRepository(
        read_results=[[{"event": _event_props(event_id, case_id), "page": [], "total": 0}]]
    )
    await get_event_with_participants(repository, case_id, event_id)
    query, _ = repository.read_calls[0]
    assert "Entity {case_id: $case_id}" in query


async def test_entity_events_filters_case_id_on_both_entity_and_event() -> None:
    case_id, entity_id = uuid4(), uuid4()
    repository = _FakeRepository(read_results=[[{"entity_id": str(entity_id)}], []])
    await get_entity_events(repository, case_id, entity_id)
    query, _ = repository.read_calls[1]
    assert "Entity {case_id: $case_id, entity_id: $entity_id}" in query
    assert "Event {case_id: $case_id}" in query


# --- Not-found behavior ------------------------------------------------------


async def test_get_evidence_provenance_not_found_raises() -> None:
    repository = _FakeRepository(read_results=[[]])
    with pytest.raises(GraphNotFoundError):
        await get_evidence_provenance(repository, uuid4(), uuid4())


async def test_get_observation_provenance_not_found_raises() -> None:
    repository = _FakeRepository(read_results=[[]])
    with pytest.raises(GraphNotFoundError):
        await get_observation_provenance(repository, uuid4(), uuid4())


async def test_get_event_with_participants_not_found_raises() -> None:
    repository = _FakeRepository(read_results=[[]])
    with pytest.raises(GraphNotFoundError):
        await get_event_with_participants(repository, uuid4(), uuid4())


def _evidence_props(evidence_id: Any, case_id: Any) -> dict[str, Any]:
    return {
        "evidence_id": str(evidence_id),
        "case_id": str(case_id),
        "source_type": "document",
        "content_type": "application/pdf",
        "object_uri": "s3://tracex-evidence/x.pdf",
        "sha256": "a" * 64,
        "classification": "restricted",
        "processing_status": "uploaded",
    }


def _event_props(event_id: Any, case_id: Any) -> dict[str, Any]:
    return {
        "event_id": str(event_id),
        "case_id": str(case_id),
        "event_type": "call",
        "review_status": "unreviewed",
        "confidence": 0.9,
        "evidence_refs": [],
    }
