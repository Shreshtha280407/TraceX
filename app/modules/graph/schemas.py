"""API-facing response shapes for the graph module's read endpoint.

Deliberately narrow: never `object_uri`, a raw evidence/document body, a
worker credential/claim token, Cypher text, or any Neo4j implementation
detail (label names, node IDs). Mirrors `evidence_lifecycle.schemas`'s
`_ResponseModel` convention exactly.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.graph.models import CaseObservationsPage, GraphRelationshipKind


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GraphEntityMentionView(_ResponseModel):
    """One evidence-local mention -- never a resolved `EntityV1`."""

    mention_id: UUID
    observation_id: UUID
    ordinal: int
    mention_type: str | None
    display_label: str


class GraphObservationView(_ResponseModel):
    """Safe projected observation view -- never a raw source body or object URI."""

    observation_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_type: str
    extraction_confidence: float
    event_time: datetime | None
    time_window_start: datetime | None
    time_window_end: datetime | None
    location_raw_text: str | None
    location_latitude: float | None
    location_longitude: float | None
    extractor_name: str
    extractor_version: str
    mentions: tuple[GraphEntityMentionView, ...]


class CaseGraphObservationsResponse(_ResponseModel):
    """One bounded, stable-ordered page of a case's projected observation graph."""

    case_id: UUID
    items: tuple[GraphObservationView, ...]
    limit: int
    offset: int
    has_more: bool


def case_graph_observations_response(page: CaseObservationsPage) -> CaseGraphObservationsResponse:
    return CaseGraphObservationsResponse(
        case_id=page.case_id,
        items=tuple(
            GraphObservationView(
                observation_id=item.observation.observation_id,
                case_id=item.observation.case_id,
                evidence_id=item.observation.evidence_id,
                observation_type=item.observation.observation_type,
                extraction_confidence=item.observation.extraction_confidence,
                event_time=item.observation.event_time,
                time_window_start=item.observation.time_window_start,
                time_window_end=item.observation.time_window_end,
                location_raw_text=item.observation.location_raw_text,
                location_latitude=item.observation.location_latitude,
                location_longitude=item.observation.location_longitude,
                extractor_name=item.observation.extractor_name,
                extractor_version=item.observation.extractor_version,
                mentions=tuple(
                    GraphEntityMentionView(
                        mention_id=mention.mention_id,
                        observation_id=mention.observation_id,
                        ordinal=mention.ordinal,
                        mention_type=mention.mention_type,
                        display_label=mention.display_label,
                    )
                    for mention in item.mentions
                ),
            )
            for item in page.items
        ),
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


# --- Gap-Closure WP-6 (G7 rest): case graph snapshot/path/analytics/motifs -----
#
# Scoped to `Entity`/`Event` nodes and the relationship kinds that connect
# them (`HAS_PARTICIPANT`, `POSSIBLY_SAME_AS`, `CONTRADICTED_BY`) -- the
# node kinds an analyst's graph view actually needs, mirroring `projection.
# py`'s own `_entity_properties`/`_event_properties` allow-lists exactly
# (never a raw Neo4j label/internal node ID, per this module's own rule
# above). Other node kinds (`Observation`, `EntityMention`, `SourceClaim`,
# `TemporalEvent`, `Case`) are evidence-provenance detail already served by
# `GET .../graph/observations`; including them here would duplicate that
# endpoint without adding analyst value.


class GraphSnapshotEntityView(_ResponseModel):
    entity_id: UUID
    entity_type: str
    canonical_label: str
    aliases: tuple[str, ...]
    review_status: str


class GraphSnapshotEventView(_ResponseModel):
    event_id: UUID
    event_type: str
    review_status: str
    confidence: float
    event_time: datetime | None


class GraphSnapshotRelationshipView(_ResponseModel):
    kind: GraphRelationshipKind
    from_id: UUID
    to_id: UUID


class GraphSnapshotResponse(_ResponseModel):
    case_id: UUID
    entities: tuple[GraphSnapshotEntityView, ...]
    events: tuple[GraphSnapshotEventView, ...]
    relationships: tuple[GraphSnapshotRelationshipView, ...]
    node_limit: int
    relationship_limit: int


class GraphPathRequest(BaseModel):
    """Exactly one of `from_entity_id`/`from_event_id`, and exactly one of
    `to_entity_id`/`to_event_id`, must be set -- a path endpoint names one
    concrete node, never both a candidate entity and a candidate event."""

    model_config = ConfigDict(extra="forbid")

    from_entity_id: UUID | None = None
    from_event_id: UUID | None = None
    to_entity_id: UUID | None = None
    to_event_id: UUID | None = None
    max_hops: int = Field(default=6, ge=1, le=15)

    @model_validator(mode="after")
    def _exactly_one_endpoint_each_side(self) -> GraphPathRequest:
        if (self.from_entity_id is None) == (self.from_event_id is None):
            raise ValueError("exactly one of from_entity_id/from_event_id is required")
        if (self.to_entity_id is None) == (self.to_event_id is None):
            raise ValueError("exactly one of to_entity_id/to_event_id is required")
        return self


class GraphPathNodeView(_ResponseModel):
    kind: str
    node_id: UUID


class GraphPathResponse(_ResponseModel):
    case_id: UUID
    found: bool
    nodes: tuple[GraphPathNodeView, ...]
    relationships: tuple[GraphSnapshotRelationshipView, ...]


class GraphAnalyticsResponse(_ResponseModel):
    case_id: UUID
    node_counts: dict[str, int]
    relationship_counts: dict[str, int]


class GraphCoParticipationMotif(_ResponseModel):
    """Two `Event`s that share at least one common `Entity` participant.

    A generic, evidence-backed structural signal -- never a scenario-
    specific narrative pattern (e.g. "call then meeting"): that kind of
    authored motif belongs to case-specific analysis, not this module.
    """

    shared_entity_id: UUID
    event_a_id: UUID
    event_b_id: UUID


class GraphMotifsResponse(_ResponseModel):
    case_id: UUID
    co_participation: tuple[GraphCoParticipationMotif, ...]
    limit: int
