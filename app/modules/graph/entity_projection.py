"""Gap-Closure WP-3 (G10): additive `POSSIBLY_SAME_AS`/`CONTRADICTED_BY`
projection for WP-2's entity-resolution candidates.

Mirrors `projection.py`'s idempotent MERGE pattern exactly. Only ever
writes a relationship between two `Entity` nodes that must already exist
(both MATCH, never MERGE -- an entity-resolution candidate can only be
projected once both of its entities are themselves projected; this never
creates an `Entity` node as a side effect). `validate_relationship_
combination` (from `taxonomy.py`) is checked before building the query, not
just documented -- an invalid combination raises rather than silently
writing a wrong edge.
"""

from __future__ import annotations

from typing import Any

from app.modules.graph.entity_models import EntityResolutionCandidateRecord
from app.modules.graph.models import GraphNodeKind, GraphRelationshipKind, ProjectionOutcome
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.taxonomy import validate_relationship_combination


def _build_possibly_same_as_query(
    candidate: EntityResolutionCandidateRecord, *, effective_status: str
) -> tuple[str, dict[str, Any]]:
    if not validate_relationship_combination(
        GraphNodeKind.ENTITY, GraphRelationshipKind.POSSIBLY_SAME_AS, GraphNodeKind.ENTITY
    ):
        raise ValueError("POSSIBLY_SAME_AS is not a valid ENTITY->ENTITY relationship")

    query = (
        "MATCH (l:Entity {case_id: $case_id, entity_id: $left_entity_id}) "
        "MATCH (r:Entity {case_id: $case_id, entity_id: $right_entity_id}) "
        "MERGE (l)-[rel:POSSIBLY_SAME_AS {"
        "entity_resolution_candidate_id: $candidate_id}]->(r) "
        "SET rel.reasons = $reasons, "
        "rel.identifier_types = $identifier_types, "
        "rel.effective_status = $effective_status, "
        "rel.config_version = $config_version "
        "RETURN rel.entity_resolution_candidate_id AS candidate_id"
    )
    params = {
        "case_id": str(candidate.case_id),
        "left_entity_id": str(candidate.left_entity_id),
        "right_entity_id": str(candidate.right_entity_id),
        "candidate_id": str(candidate.entity_resolution_candidate_id),
        "reasons": [r.value for r in candidate.reasons],
        "identifier_types": list(candidate.identifier_types),
        "effective_status": effective_status,
        "config_version": candidate.config_version,
    }
    return query, params


def _build_contradicted_by_query(
    candidate: EntityResolutionCandidateRecord,
) -> tuple[str, dict[str, Any]]:
    if not validate_relationship_combination(
        GraphNodeKind.ENTITY, GraphRelationshipKind.CONTRADICTED_BY, GraphNodeKind.ENTITY
    ):
        raise ValueError("CONTRADICTED_BY is not a valid ENTITY->ENTITY relationship")

    query = (
        "MATCH (l:Entity {case_id: $case_id, entity_id: $left_entity_id}) "
        "MATCH (r:Entity {case_id: $case_id, entity_id: $right_entity_id}) "
        "MERGE (l)-[rel:CONTRADICTED_BY {"
        "entity_resolution_candidate_id: $candidate_id}]->(r) "
        "SET rel.contradiction_reasons = $contradiction_reasons "
        "RETURN rel.entity_resolution_candidate_id AS candidate_id"
    )
    params = {
        "case_id": str(candidate.case_id),
        "left_entity_id": str(candidate.left_entity_id),
        "right_entity_id": str(candidate.right_entity_id),
        "candidate_id": str(candidate.entity_resolution_candidate_id),
        "contradiction_reasons": list(candidate.contradiction_reasons),
    }
    return query, params


async def project_entity_resolution_candidate(
    repository: Neo4jGraphRepository,
    candidate: EntityResolutionCandidateRecord,
    *,
    effective_status: str,
) -> ProjectionOutcome:
    """Project a `POSSIBLY_SAME_AS` edge, plus a `CONTRADICTED_BY` edge if the
    candidate carries any recorded contradiction. `DEFERRED` (never a crash
    or a fabricated edge) if either entity hasn't been projected yet --
    matches `project_event`'s own "all dependencies must exist first" rule.
    """
    query, params = _build_possibly_same_as_query(candidate, effective_status=effective_status)
    rows = await repository.write(query, params)
    if not rows:
        return ProjectionOutcome.DEFERRED

    if candidate.contradiction_reasons:
        contradiction_query, contradiction_params = _build_contradicted_by_query(candidate)
        await repository.write(contradiction_query, contradiction_params)

    return ProjectionOutcome.APPLIED


__all__ = ["project_entity_resolution_candidate"]
