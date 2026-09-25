"""Gap-Closure follow-up: composes WP-2's entity/entity-resolution-candidate
Postgres records with the already-existing, already-tested `project_entity`/
`project_entity_resolution_candidate` projection functions.

Neither `entity_service.py` (WP-2, deliberately Postgres-only -- see its own
module docstring) nor `projection.py`/`entity_projection.py` (deliberately
pure projection functions, no orchestration) call each other. Before this
module, nothing did: `entity_service.py` created real entities and
candidates in Postgres, but no caller anywhere in this repository ever
invoked `project_entity`/`project_entity_resolution_candidate` for that real
data -- see `docs/qa/known-limitations.md`'s "WP-2 has no Neo4j footprint at
all, by design" entry, closed by this module's two real callers
(`intelligence_worker.resolve_entities_once`, `entity_api.
submit_entity_resolution_review`).

`EventV1`/`project_event` are deliberately out of scope here: no service in
this codebase constructs a real `EventV1` yet (see `docs/architecture/
graph-taxonomy-v1.md`) -- there is no real event data to project, and
inventing one would be fabricated data, not a wiring fix.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.contracts.entity import EntityV1
from app.modules.graph.entity_models import (
    EntityResolutionCandidateRecord,
    entity_resolution_review_view,
)
from app.modules.graph.entity_projection import project_entity_resolution_candidate
from app.modules.graph.entity_repository import EntityRepository
from app.modules.graph.models import ProjectionOutcome
from app.modules.graph.projection import project_entity
from app.modules.graph.repository import Neo4jGraphRepository


@dataclass(frozen=True)
class EntityGraphSyncSummary:
    """Real counts from one sync pass. `deferred_candidate_count` is a
    normal outcome, not an error: it means a candidate's own two entities
    weren't both projected in this pass (e.g. one had no retrieval-eligible
    signal) -- it applies automatically on a later pass once both exist."""

    projected_entity_count: int
    applied_candidate_count: int
    deferred_candidate_count: int


async def project_case_entities(
    graph_repository: Neo4jGraphRepository, entities: Iterable[EntityV1]
) -> int:
    """MERGE every entity into Neo4j. Idempotent -- safe to re-run for
    already-projected entities. Returns the count written."""
    count = 0
    for entity in entities:
        await project_entity(graph_repository, entity)
        count += 1
    return count


async def project_case_candidates(
    graph_repository: Neo4jGraphRepository,
    entity_repository: EntityRepository,
    case_id: UUID,
    candidates: Iterable[EntityResolutionCandidateRecord],
) -> tuple[int, int]:
    """Project each candidate's `POSSIBLY_SAME_AS`/`CONTRADICTED_BY` edges
    using its real, current effective status -- the same computation
    `GET /cases/{id}/entity-candidates` uses to display it, so the graph
    edge and the review-listing view never disagree. Returns
    `(applied_count, deferred_count)`.
    """
    applied = deferred = 0
    for candidate in candidates:
        decisions = await entity_repository.list_decisions(
            case_id, candidate.entity_resolution_candidate_id
        )
        view = entity_resolution_review_view(candidate, tuple(decisions))
        outcome = await project_entity_resolution_candidate(
            graph_repository, candidate, effective_status=view.effective_status
        )
        if outcome is ProjectionOutcome.APPLIED:
            applied += 1
        else:
            deferred += 1
    return applied, deferred


async def sync_case_entities_and_candidates(
    graph_repository: Neo4jGraphRepository,
    entity_repository: EntityRepository,
    case_id: UUID,
    entities: Sequence[EntityV1],
    candidates: Sequence[EntityResolutionCandidateRecord],
) -> EntityGraphSyncSummary:
    """Entities first, then candidates -- `project_entity_resolution_
    candidate` only `MATCH`es (never `MERGE`s) its two entities, so this
    order is load-bearing, not stylistic."""
    projected_entity_count = await project_case_entities(graph_repository, entities)
    applied, deferred = await project_case_candidates(
        graph_repository, entity_repository, case_id, candidates
    )
    return EntityGraphSyncSummary(
        projected_entity_count=projected_entity_count,
        applied_candidate_count=applied,
        deferred_candidate_count=deferred,
    )


__all__ = [
    "EntityGraphSyncSummary",
    "project_case_candidates",
    "project_case_entities",
    "sync_case_entities_and_candidates",
]
