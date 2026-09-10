"""GRAPH-ISOLATION-001 (live): case-scoped identity holds under real Neo4j constraints.

Two cases deliberately reuse the same `entity_id` -- the worst-case
collision the compound `(case_id, entity_id)` uniqueness constraint
(`app/modules/graph/schema.py`) exists to prevent. Confirms they never
share graph identity, and that a case-scoped query cannot retrieve the
other case's event/participant data. Self-skips if Neo4j is unavailable
(see `conftest.py`).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.graph.errors import GraphNotFoundError
from app.modules.graph.projection import (
    project_entity,
    project_event,
    project_evidence,
    project_observation,
)
from app.modules.graph.queries import get_case_graph_summary, get_event_with_participants
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.schema import apply_schema
from tests.fixtures.graph.factories import make_linked_graph_fixture


async def test_same_entity_id_reused_across_two_cases_stays_isolated(
    repository: Neo4jGraphRepository,
) -> None:
    await apply_schema(repository)
    case_a, case_b = uuid4(), uuid4()
    shared_entity_id = uuid4()

    try:
        fixture_a = make_linked_graph_fixture(case_id=case_a, entity_id=shared_entity_id)
        fixture_b = make_linked_graph_fixture(case_id=case_b, entity_id=shared_entity_id)

        for fixture in (fixture_a, fixture_b):
            await project_evidence(repository, fixture.evidence)
            await project_observation(repository, fixture.observation)
            await project_entity(repository, fixture.entity)
            await project_event(repository, fixture.event)

        summary_a = await get_case_graph_summary(repository, case_a)
        summary_b = await get_case_graph_summary(repository, case_b)
        assert summary_a.entity_count == 1
        assert summary_b.entity_count == 1

        # Exactly two distinct `Entity` nodes exist for the shared entity_id: one per case.
        rows = await repository.read(
            "MATCH (n:Entity {entity_id: $entity_id}) RETURN n.case_id AS case_id",
            {"entity_id": str(shared_entity_id)},
        )
        assert {row["case_id"] for row in rows} == {str(case_a), str(case_b)}
        assert len(rows) == 2

        # Case A's event/participant query returns only case A's data.
        event_view_a = await get_event_with_participants(
            repository, case_a, fixture_a.event.event_id
        )
        assert event_view_a.total_participants == 1
        assert event_view_a.participants[0].case_id == case_a

        # Case B cannot retrieve case A's event through its own case-scoped query.
        with pytest.raises(GraphNotFoundError):
            await get_event_with_participants(repository, case_b, fixture_a.event.event_id)
    finally:
        await repository.write(
            "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_a)}
        )
        await repository.write(
            "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_b)}
        )
