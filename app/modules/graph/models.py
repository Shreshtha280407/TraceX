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
    ENTITY_MENTION = "EntityMention"
    SOURCE_CLAIM = "SourceClaim"
    TEMPORAL_EVENT = "TemporalEvent"
    CORRELATION = "Correlation"
    HYPOTHESIS = "Hypothesis"


class GraphRelationshipKind(StrEnum):
    """Relationship types written by `projection.py`. Matches `graph-taxonomy-v1.md`.

    There is deliberately no *evidentiary* entity-to-entity kind in this
    enum: TraceX never models a call/meeting/transaction/sighting/message
    as a direct, timeless edge between two `Entity` nodes -- every such
    connection is mediated by a time-bounded `Event` via `HAS_PARTICIPANT`.
    There is likewise no `EntityMention`-to-`EntityMention` or
    `EntityMention`-to-`Entity` kind: a mention is evidence-local only (see
    `EntityMentionNode`) -- resolving it into a real `Entity` is later-phase
    entity-resolution work.

    `POSSIBLY_SAME_AS`/`CONTRADICTED_BY` (Gap-Closure WP-3, G10) are the
    one deliberate exception, and are not evidentiary edges at all: they
    are identity-*resolution* review metadata (WP-2, ADR-020) -- "these two
    `Entity` nodes might be the same identity, pending human review", never
    an assertion that the underlying identities interacted. They still
    never assert or imply a merge; only an explicit `EntityReviewDecisionRecord`
    (never this edge's mere existence) can do that. See
    `docs/decisions/ADR-021-graph-taxonomy-alignment.md`.
    """

    HAS_EVIDENCE = "HAS_EVIDENCE"
    YIELDED_OBSERVATION = "YIELDED_OBSERVATION"
    HAS_OBSERVATION = "HAS_OBSERVATION"
    HAS_ENTITY = "HAS_ENTITY"
    HAS_EVENT = "HAS_EVENT"
    SUPPORTS = "SUPPORTS"
    HAS_PARTICIPANT = "HAS_PARTICIPANT"
    MENTIONS = "MENTIONS"
    PROJECTS_CLAIM = "PROJECTS_CLAIM"
    PROJECTS_EVENT = "PROJECTS_EVENT"
    HAS_CLAIM_PARTICIPANT = "HAS_CLAIM_PARTICIPANT"
    SUPPORTED_BY_OBSERVATION = "SUPPORTED_BY_OBSERVATION"
    REFERENCES_CANDIDATE = "REFERENCES_CANDIDATE"
    #: Gap-Closure WP-3 (G10) additions -- see `docs/decisions/
    #: ADR-021-graph-taxonomy-alignment.md`. Each is written only where a
    #: real durable record backs it; none is a fabricated edge.
    POSSIBLY_SAME_AS = "POSSIBLY_SAME_AS"
    CANDIDATE_ASSOCIATION = "CANDIDATE_ASSOCIATION"
    CONTRADICTED_BY = "CONTRADICTED_BY"


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


class GraphProjectionJobStatus(StrEnum):
    """Lifecycle status of a durable `graph_projection_jobs` row.

    Mirrors `app.contracts.worker.WorkerStatus`'s shape but is a distinct
    enum: this is PostgreSQL-outbox status for "has this observation been
    projected into Neo4j", not a `WorkerResultV1` extraction outcome.
    `QUEUED` and `DEFERRED` are both claimable (see
    `outbox_repository.claim_batch`) -- `DEFERRED` exists only to
    distinguish, for operator inspection, "this attempt found a dependency
    not ready yet" from `QUEUED`'s "never attempted, or requeued after a
    transient Neo4j failure". Both are automatically retried the same way;
    the distinction is diagnostic, not behavioral.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"


#: Claimable statuses -- eligible for `outbox_repository.claim_batch` when
#: not currently `RUNNING` with a live lease. See `GraphProjectionJobStatus`.
CLAIMABLE_PROJECTION_JOB_STATUSES = frozenset(
    {GraphProjectionJobStatus.QUEUED, GraphProjectionJobStatus.DEFERRED}
)

#: Terminal statuses -- a job here is never automatically reclaimed again.
TERMINAL_PROJECTION_JOB_STATUSES = frozenset(
    {GraphProjectionJobStatus.SUCCEEDED, GraphProjectionJobStatus.FAILED}
)


class GraphProjectionJobRecord(GraphModel):
    """A full `graph_projection_jobs` row (see `outbox_repository.py`).

    PostgreSQL is the durable source of truth for "this canonical
    observation needs (re)projecting" -- Neo4j itself is a derived,
    rebuildable projection. See `docs/architecture/graph-projection.md`.
    """

    projection_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    status: GraphProjectionJobStatus
    attempt: int
    max_attempts: int
    lease_expires_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class MediaProjectionLineage(GraphModel):
    """Safe, persisted Phase 4 lineage associated with one observation.

    This is deliberately identifiers and version metadata only.  In
    particular, derived-artifact object references are not copied from the
    evidence-lifecycle store into Neo4j.
    """

    chunk_id: UUID
    manifest_id: UUID
    manifest_hash: str
    processor_version: str
    configuration_hash: str
    artifact_ids: tuple[UUID, ...] = ()


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


class EntityMentionProjectionResult(GraphModel):
    """Result of `project_observation_mentions` -- a batch op over one observation's mentions.

    Does not subclass `ProjectionResult`: unlike every other projection call,
    this one may write zero, one, or many `EntityMention` nodes in a single
    call, so there is no single `domain_id` to report. `missing_observation_id`
    is set only when `outcome` is `DEFERRED`: the parent `observation_id` has
    not been projected into this case yet, so no mention was written at all.
    """

    outcome: ProjectionOutcome
    case_id: UUID
    observation_id: UUID
    mention_count: int = 0
    missing_observation_id: UUID | None = None


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


class EntityMentionNode(GraphModel):
    """Read-view of a projected `EntityMention` node, plus its `MENTIONS` edge's `ordinal`.

    Evidence-local only -- never a resolved `Entity`. `mention_type` mirrors
    `ExtractedEntityMention.entity_type_hint` verbatim (a free-form hint, not
    a validated taxonomy value); `display_label` mirrors `.text` exactly.
    Deliberately excludes `ExtractedEntityMention.attributes` (an
    open-ended bag), same exclusion policy as `ObservationV1.attributes`/
    `EntityV1.attributes`/`EventV1.attributes`.
    """

    mention_id: UUID
    case_id: UUID
    observation_id: UUID
    mention_type: str | None = None
    display_label: str
    ordinal: int


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


class ObservationWithMentions(GraphModel):
    """One `Observation` plus the (bounded) `EntityMention`s it yielded, ordinal-ordered."""

    observation: ObservationNode
    mentions: tuple[EntityMentionNode, ...]


class CaseObservationsPage(GraphModel):
    """Result of `list_case_observations`: one bounded page, stable-ordered by `observation_id`."""

    case_id: UUID
    items: tuple[ObservationWithMentions, ...]
    limit: int
    offset: int
    has_more: bool
