"""API-facing response shapes for the graph module's read endpoint.

Deliberately narrow: never `object_uri`, a raw evidence/document body, a
worker credential/claim token, Cypher text, or any Neo4j implementation
detail (label names, node IDs). Mirrors `evidence_lifecycle.schemas`'s
`_ResponseModel` convention exactly.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.graph.models import CaseObservationsPage


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
