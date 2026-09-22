"""Gap-Closure WP-3 (G10): a taxonomy validator, additive to the graph module.

Lives here, not in `app/contracts/entity.py`/`app/contracts/event.py`: those
are frozen V1 contracts that deliberately keep `entity_type`/`event_type`
as open strings ("the detailed entity/relationship taxonomy is owned by a
later phase" -- `EntityV1`'s own docstring). This module is that later
phase's *recommended* vocabulary, checked here at projection time, never
inside the frozen contracts themselves.

Two different strengths of check, by design:

- **Relationship node-kind combinations are strictly enforced.**
  `GraphRelationshipKind`/`GraphNodeKind` are this module's own closed
  enums (not open strings), so there is no legitimate "unknown" combination
  to tolerate -- `validate_relationship_combination` returns `False` for
  anything not in `ALLOWED_RELATIONSHIP_NODE_KINDS`, and callers must not
  write a relationship it rejects.
- **`entity_type`/`event_type` recommendations are advisory only.**
  `RECOMMENDED_ENTITY_TYPES` is sourced from the identifier kinds
  `graph.intelligence.retrieval._IDENTIFIER_TYPES` already recognizes
  (the only real, evidence-backed vocabulary that exists today) plus this
  module's own `"alias"`/`"unknown"` fallbacks. An entity/event whose type
  isn't in the recommended set still projects normally -- `is_recommended_
  entity_type`/`is_recommended_event_type` exist only so a caller can log a
  safe, non-fatal warning (the plan's own "unknown types route to the
  documented `unknown` escape hatch with a safe warning" requirement),
  never to reject a valid `EntityV1`/`EventV1`.
"""

from __future__ import annotations

from app.modules.graph.models import GraphNodeKind, GraphRelationshipKind

#: Sourced from `graph.intelligence.retrieval._IDENTIFIER_TYPES` (the only
#: real, evidence-backed identifier vocabulary in this codebase today) plus
#: this module's own fallbacks (`entity_service._entity_type_and_label`
#: uses `"alias"` when only an alias exists, `"unknown"` when neither an
#: identifier nor an alias exists -- the latter never actually reaches an
#: `EntityV1` today, since `create_entities_for_case` skips observations
#: with no identity signal at all, but is listed for completeness).
RECOMMENDED_ENTITY_TYPES = frozenset(
    {
        "phone",
        "email",
        "vehicle_registration",
        "device_id",
        "account",
        "platform_handle",
        "alias",
        "unknown",
    }
)

#: No `EventV1` is instantiated anywhere in this codebase yet (see
#: `docs/qa/known-limitations.md`'s "WP-3" section) -- there is no real,
#: evidence-backed event-type vocabulary to source from the way
#: `RECOMMENDED_ENTITY_TYPES` sources one from `retrieval._IDENTIFIER_TYPES`.
#: Left empty deliberately rather than inventing values with no grounding;
#: `is_recommended_event_type` always logs-advisory (never enforces) until
#: a later phase's real event projection establishes one.
RECOMMENDED_EVENT_TYPES: frozenset[str] = frozenset()

UNKNOWN_TYPE_ESCAPE_HATCH = "unknown"

#: The exactly-allowed `(from_node_kind, to_node_kind)` pair for every
#: `GraphRelationshipKind` this module knows about -- both the pre-existing
#: kinds (`projection.py`, unchanged) and the three WP-3 additions
#: (`POSSIBLY_SAME_AS`, `CANDIDATE_ASSOCIATION`, `CONTRADICTED_BY`). A
#: relationship kind or a node-kind pair not listed here is rejected by
#: `validate_relationship_combination` -- never silently allowed.
ALLOWED_RELATIONSHIP_NODE_KINDS: dict[
    GraphRelationshipKind, tuple[GraphNodeKind, GraphNodeKind]
] = {
    GraphRelationshipKind.HAS_EVIDENCE: (GraphNodeKind.CASE, GraphNodeKind.EVIDENCE),
    GraphRelationshipKind.YIELDED_OBSERVATION: (GraphNodeKind.EVIDENCE, GraphNodeKind.OBSERVATION),
    GraphRelationshipKind.HAS_OBSERVATION: (GraphNodeKind.CASE, GraphNodeKind.OBSERVATION),
    GraphRelationshipKind.HAS_ENTITY: (GraphNodeKind.CASE, GraphNodeKind.ENTITY),
    GraphRelationshipKind.HAS_EVENT: (GraphNodeKind.CASE, GraphNodeKind.EVENT),
    GraphRelationshipKind.SUPPORTS: (GraphNodeKind.OBSERVATION, GraphNodeKind.EVENT),
    GraphRelationshipKind.HAS_PARTICIPANT: (GraphNodeKind.EVENT, GraphNodeKind.ENTITY),
    GraphRelationshipKind.MENTIONS: (GraphNodeKind.OBSERVATION, GraphNodeKind.ENTITY_MENTION),
    GraphRelationshipKind.PROJECTS_CLAIM: (GraphNodeKind.CASE, GraphNodeKind.SOURCE_CLAIM),
    GraphRelationshipKind.PROJECTS_EVENT: (GraphNodeKind.CASE, GraphNodeKind.TEMPORAL_EVENT),
    GraphRelationshipKind.HAS_CLAIM_PARTICIPANT: (GraphNodeKind.SOURCE_CLAIM, GraphNodeKind.ENTITY),
    GraphRelationshipKind.SUPPORTED_BY_OBSERVATION: (
        GraphNodeKind.SOURCE_CLAIM,
        GraphNodeKind.OBSERVATION,
    ),
    GraphRelationshipKind.REFERENCES_CANDIDATE: (GraphNodeKind.CASE, GraphNodeKind.CORRELATION),
    # WP-3 additions (G10) -- each backed by a real durable record:
    # `POSSIBLY_SAME_AS` by `entity_repository.entity_resolution_candidates`
    # (WP-2), `CONTRADICTED_BY` by that same record's own
    # `contradiction_reasons` field. `CANDIDATE_ASSOCIATION` is reserved for
    # Phase 5's `correlation_candidate_links` between two `Observation`
    # nodes -- documented here, not yet wired into `integration_projector.py`
    # (out of this WP's scope; see `docs/qa/known-limitations.md`).
    GraphRelationshipKind.POSSIBLY_SAME_AS: (GraphNodeKind.ENTITY, GraphNodeKind.ENTITY),
    GraphRelationshipKind.CONTRADICTED_BY: (GraphNodeKind.ENTITY, GraphNodeKind.ENTITY),
    GraphRelationshipKind.CANDIDATE_ASSOCIATION: (
        GraphNodeKind.OBSERVATION,
        GraphNodeKind.OBSERVATION,
    ),
}


def validate_relationship_combination(
    from_kind: GraphNodeKind, relationship_kind: GraphRelationshipKind, to_kind: GraphNodeKind
) -> bool:
    """Strict: `True` only for exactly the allowed `(from_kind, to_kind)` pair."""
    allowed = ALLOWED_RELATIONSHIP_NODE_KINDS.get(relationship_kind)
    return allowed is not None and allowed == (from_kind, to_kind)


def is_recommended_entity_type(entity_type: str) -> bool:
    """Advisory only -- `False` means "log a safe warning", never "reject"."""
    return entity_type in RECOMMENDED_ENTITY_TYPES


def is_recommended_event_type(event_type: str) -> bool:
    """Advisory only. Always `False` today -- see `RECOMMENDED_EVENT_TYPES`'s docstring."""
    return event_type in RECOMMENDED_EVENT_TYPES


__all__ = [
    "ALLOWED_RELATIONSHIP_NODE_KINDS",
    "RECOMMENDED_ENTITY_TYPES",
    "RECOMMENDED_EVENT_TYPES",
    "UNKNOWN_TYPE_ESCAPE_HATCH",
    "is_recommended_entity_type",
    "is_recommended_event_type",
    "validate_relationship_combination",
]
