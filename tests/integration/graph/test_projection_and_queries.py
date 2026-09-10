"""Live projection + provenance-query integration tests (self-skip if Neo4j unavailable).

Covers GRAPH-PROJECTION-001, GRAPH-PROJECTION-002, and GRAPH-PROVENANCE-001:
a minimal evidence -> observation -> entity -> event fixture projects to the
exact labels/relationships the taxonomy defines, and re-projecting the same
fixture creates no duplicates. See `conftest.py` for the skip condition.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.modules.graph.models import ProjectionOutcome, ProjectionResult
from app.modules.graph.projection import (
    project_entity,
    project_event,
    project_evidence,
    project_observation,
)
from app.modules.graph.queries import (
    get_case_graph_summary,
    get_event_with_participants,
    get_evidence_provenance,
    get_observation_provenance,
)
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.schema import apply_schema
from tests.fixtures.graph.factories import LinkedGraphFixture, make_linked_graph_fixture


async def _project_all(
    repository: Neo4jGraphRepository, fixture: LinkedGraphFixture
) -> Sequence[ProjectionResult]:
    return (
        await project_evidence(repository, fixture.evidence),
        await project_observation(repository, fixture.observation),
        await project_entity(repository, fixture.entity),
        await project_event(repository, fixture.event),
    )


async def test_projecting_full_chain_creates_expected_labels_and_relationships(
    repository: Neo4jGraphRepository, case_id: UUID
) -> None:
    await apply_schema(repository)
    fixture = make_linked_graph_fixture(case_id=case_id)

    results = await _project_all(repository, fixture)
    assert all(r.outcome == ProjectionOutcome.APPLIED for r in results)

    rows = await repository.read(
        "MATCH (c:Case {case_id: $case_id}) "
        "MATCH (c)-[:HAS_EVIDENCE]->(e:Evidence {case_id: $case_id}) "
        "MATCH (e)-[:YIELDED_OBSERVATION]->(o:Observation {case_id: $case_id}) "
        "MATCH (c)-[:HAS_OBSERVATION]->(o) "
        "MATCH (c)-[:HAS_ENTITY]->(n:Entity {case_id: $case_id}) "
        "MATCH (c)-[:HAS_EVENT]->(v:Event {case_id: $case_id}) "
        "MATCH (v)-[:HAS_PARTICIPANT]->(n) "
        "RETURN count(*) AS matches",
        {"case_id": str(case_id)},
    )
    assert rows[0]["matches"] == 1

    summary = await get_case_graph_summary(repository, case_id)
    assert summary.evidence_count == 1
    assert summary.observation_count == 1
    assert summary.entity_count == 1
    assert summary.event_count == 1

    evidence_provenance = await get_evidence_provenance(
        repository, case_id, fixture.evidence.evidence_id
    )
    assert evidence_provenance.evidence.evidence_id == fixture.evidence.evidence_id
    assert evidence_provenance.total_observations == 1
    assert evidence_provenance.observations[0].observation_id == fixture.observation.observation_id

    observation_provenance = await get_observation_provenance(
        repository, case_id, fixture.observation.observation_id
    )
    assert observation_provenance.evidence is not None
    assert observation_provenance.evidence.evidence_id == fixture.evidence.evidence_id
    assert (
        observation_provenance.observation.source_locator.page
        == fixture.observation.source_locator.page
    )

    event_view = await get_event_with_participants(repository, case_id, fixture.event.event_id)
    assert event_view.total_participants == 1
    assert event_view.participants[0].entity_id == fixture.entity.entity_id


async def test_repeated_projection_creates_no_duplicates(
    repository: Neo4jGraphRepository, case_id: UUID
) -> None:
    await apply_schema(repository)
    fixture = make_linked_graph_fixture(case_id=case_id)

    await _project_all(repository, fixture)
    second_results = await _project_all(repository, fixture)
    assert all(r.outcome == ProjectionOutcome.APPLIED for r in second_results)

    node_rows = await repository.read(
        "MATCH (n {case_id: $case_id}) RETURN labels(n) AS labels, count(n) AS count",
        {"case_id": str(case_id)},
    )
    assert len(node_rows) == 5, "expected exactly Case/Evidence/Observation/Entity/Event"
    for row in node_rows:
        assert row["count"] == 1, f"duplicate node(s) for labels {row['labels']}"

    relationship_rows = await repository.read(
        "MATCH (a {case_id: $case_id})-[r]->(b {case_id: $case_id}) "
        "RETURN type(r) AS rel_type, count(r) AS count",
        {"case_id": str(case_id)},
    )
    assert len(relationship_rows) == 6, (
        "expected HAS_EVIDENCE/YIELDED_OBSERVATION/HAS_OBSERVATION/"
        "HAS_ENTITY/HAS_EVENT/HAS_PARTICIPANT, one each"
    )
    for row in relationship_rows:
        assert row["count"] == 1, f"duplicate relationship(s) of type {row['rel_type']}"
