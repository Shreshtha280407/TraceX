"""Typed internal domain models for the graph module.

None of these are public TraceX contracts -- they exist only inside
`app/modules/graph/` to give the projection and query layers typed inputs
and outputs instead of passing raw `dict[str, Any]` Neo4j records around.
See `docs/architecture/graph-taxonomy-v1.md` for the node/relationship
vocabulary these enums encode.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GraphModel(BaseModel):
    """Base class for internal graph-module models: immutable, no stray fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GraphNodeKind(StrEnum):
    """Node labels written by `projection.py`. Matches `graph-taxonomy-v1.md`."""

    CASE = "Case"
    EVIDENCE = "Evidence"
    OBSERVATION = "Observation"
    ENTITY = "Entity"
    EVENT = "Event"


class GraphRelationshipKind(StrEnum):
    """Relationship types written by `projection.py`. Matches `graph-taxonomy-v1.md`.

    There is deliberately no entity-to-entity kind in this enum: TraceX never
    models a call/meeting/transaction/sighting/message as a direct, timeless
    edge between two `Entity` nodes -- every such connection is mediated by a
    time-bounded `Event` via `HAS_PARTICIPANT`.
    """

    HAS_EVIDENCE = "HAS_EVIDENCE"
    YIELDED_OBSERVATION = "YIELDED_OBSERVATION"
    HAS_OBSERVATION = "HAS_OBSERVATION"
    HAS_ENTITY = "HAS_ENTITY"
    HAS_EVENT = "HAS_EVENT"
    SUPPORTS = "SUPPORTS"
    HAS_PARTICIPANT = "HAS_PARTICIPANT"


class AssertionKind(StrEnum):
    """The nature of a `SUPPORTS` edge's claim.

    Only `FACT` exists in this phase: a direct, faithful projection of a
    canonical observation. Inference/hypothesis kinds belong to a later
    correlation/hypothesis-engine phase and must not be created here.
    """

    FACT = "fact"


class ProjectionOutcome(StrEnum):
    """Whether a projection call wrote its target node, or had to defer."""

    APPLIED = "applied"
    DEFERRED = "deferred"


class ProjectionResult(GraphModel):
    """Summary of a single idempotent projection call."""

    outcome: ProjectionOutcome
    node_kind: GraphNodeKind
    case_id: UUID
    domain_id: UUID
    relationships_upserted: int = 0


class ObservationProjectionResult(ProjectionResult):
    """Result of `project_observation`.

    `missing_evidence_id` is set only when `outcome` is `DEFERRED`: the
    observation's `evidence_id` has not been projected into this case yet,
    so no `Observation` node or relationship was written (see
    `docs/decisions/ADR-001-graph-projection-and-case-isolation.md`).
    """

    missing_evidence_id: UUID | None = None


class EventProjectionResult(ProjectionResult):
    """Result of `project_event`.

    `missing_participant_entity_ids` is non-empty only when `outcome` is
    `DEFERRED`: one or more `participant_entity_ids` have not been projected
    as `Entity` nodes in this case yet. When any are missing, no `Event`
    node or relationship is written at all -- projection is all-or-nothing
    per event, so a queryable `Event` never has a partial participant list.
    """

    missing_participant_entity_ids: tuple[UUID, ...] = ()


class SourceLocatorRef(GraphModel):
    """Read-view of a stored `SourceLocator` (see `app/contracts/common.py`).

    All fields optional: this mirrors already-validated, already-stored
    data read back from Neo4j, not a value being constructed and validated
    for the first time, so `SourceLocator`'s own "at least one field set"
    invariant is not re-checked here.
    """

    page: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    bbox_x_min: float | None = None
    bbox_y_min: float | None = None
    bbox_x_max: float | None = None
    bbox_y_max: float | None = None
    sheet: str | None = None
    row: int | None = None
    column: int | None = None
    json_path: str | None = None
    frame_number: int | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    message_id: str | None = None


class EvidenceNode(GraphModel):
    """Read-view of a projected `Evidence` node -- the exact allow-listed property set."""

    evidence_id: UUID
    case_id: UUID
    source_type: str
    content_type: str
    object_uri: str
    sha256: str
    classification: str
    processing_status: str


class ObservationNode(GraphModel):
    """Read-view of a projected `Observation` node."""

    observation_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_type: str
    event_time: datetime | None = None
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    location_raw_text: str | None = None
    location_latitude: float | None = None
    location_longitude: float | None = None
    location_precision_meters: float | None = None
    extraction_confidence: float
    source_locator: SourceLocatorRef
    extractor_name: str
    extractor_version: str
    extractor_config_hash: str
    extractor_model_version: str


class EntityNode(GraphModel):
    """Read-view of a projected `Entity` node."""

    entity_id: UUID
    case_id: UUID
    entity_type: str
    canonical_label: str
    aliases: tuple[str, ...] = ()
    stable_identifiers_json: str
    review_status: str
    created_from_observation_ids: tuple[UUID, ...] = ()


class EventNode(GraphModel):
    """Read-view of a projected `Event` node."""

    event_id: UUID
    case_id: UUID
    event_type: str
    event_time: datetime | None = None
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    review_status: str
    confidence: float
    evidence_refs: tuple[UUID, ...] = ()


class CaseGraphSummary(GraphModel):
    """Result of `get_case_graph_summary`."""

    case_id: UUID
    evidence_count: int
    observation_count: int
    entity_count: int
    event_count: int


class EvidenceProvenance(GraphModel):
    """Result of `get_evidence_provenance`."""

    evidence: EvidenceNode
    observations: tuple[ObservationNode, ...]
    total_observations: int
    truncated: bool


class ObservationProvenance(GraphModel):
    """Result of `get_observation_provenance`."""

    observation: ObservationNode
    evidence: EvidenceNode | None


class EventWithParticipants(GraphModel):
    """Result of `get_event_with_participants`."""

    event: EventNode
    participants: tuple[EntityNode, ...]
    total_participants: int
    truncated: bool


class EntityEventsPage(GraphModel):
    """Result of `get_entity_events`: one bounded page, oldest-first by `event_time`."""

    entity_id: UUID
    events: tuple[EventNode, ...]
    limit: int
    offset: int
    has_more: bool
