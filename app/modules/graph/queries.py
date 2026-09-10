"""Safe, case-scoped, bounded read queries over the projected graph.

Every public function here takes `case_id` as its first argument and filters
by it in every `MATCH` in its Cypher -- never only on the first node in the
pattern -- so a query can never traverse from a case-scoped anchor node into
a different case's data through a relationship. Every query is fully literal
Cypher text with `$name` placeholders only: no ID, label, or other value is
ever interpolated into a query string.

No function here returns an unbounded result set: multi-row results are
capped (see `MAX_COLLECTION_LIMIT`, `MAX_PAGE_SIZE`) and report whether more
data exists (`truncated` / `has_more`) rather than silently dropping it or
attempting to return everything.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from app.modules.graph.errors import GraphNotFoundError, GraphValidationError
from app.modules.graph.models import (
    CaseGraphSummary,
    EntityEventsPage,
    EntityNode,
    EventNode,
    EventWithParticipants,
    EvidenceNode,
    EvidenceProvenance,
    ObservationNode,
    ObservationProvenance,
    SourceLocatorRef,
)
from app.modules.graph.repository import Neo4jGraphRepository

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_COLLECTION_LIMIT = 500


def _ensure_case_id(case_id: UUID | None) -> UUID:
    """Reject a missing/falsy `case_id` before it reaches any query.

    A real `UUID` instance (including the nil UUID) is always truthy in
    Python, so this only ever rejects `None` or an empty/falsy value a
    caller passed despite the type hint -- it never rejects a legitimate ID.
    """
    if not case_id:
        raise GraphValidationError("case_id is required")
    return case_id


def _ensure_limit(limit: int) -> int:
    if limit <= 0 or limit > MAX_PAGE_SIZE:
        raise GraphValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


def _ensure_offset(offset: int) -> int:
    if offset < 0:
        raise GraphValidationError("offset must be >= 0")
    return offset


def _to_datetime(value: Any) -> datetime | None:
    # Localized driver-boundary `Any`: a Neo4j temporal property comes back
    # as `neo4j.time.DateTime`, not `datetime.datetime`; `to_native()`
    # converts it. Already-`None` or already-native values pass through.
    if value is None:
        return None
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        native: datetime = to_native()
        return native
    return value  # type: ignore[no-any-return]


def _evidence_node_from_props(props: Mapping[str, Any]) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=UUID(props["evidence_id"]),
        case_id=UUID(props["case_id"]),
        source_type=props["source_type"],
        content_type=props["content_type"],
        object_uri=props["object_uri"],
        sha256=props["sha256"],
        classification=props["classification"],
        processing_status=props["processing_status"],
    )


def _observation_node_from_props(props: Mapping[str, Any]) -> ObservationNode:
    locator = SourceLocatorRef(
        page=props.get("source_locator_page"),
        span_start=props.get("source_locator_span_start"),
        span_end=props.get("source_locator_span_end"),
        bbox_x_min=props.get("source_locator_bbox_x_min"),
        bbox_y_min=props.get("source_locator_bbox_y_min"),
        bbox_x_max=props.get("source_locator_bbox_x_max"),
        bbox_y_max=props.get("source_locator_bbox_y_max"),
        sheet=props.get("source_locator_sheet"),
        row=props.get("source_locator_row"),
        column=props.get("source_locator_column"),
        json_path=props.get("source_locator_json_path"),
        frame_number=props.get("source_locator_frame_number"),
        time_start_ms=props.get("source_locator_time_start_ms"),
        time_end_ms=props.get("source_locator_time_end_ms"),
        message_id=props.get("source_locator_message_id"),
    )
    return ObservationNode(
        observation_id=UUID(props["observation_id"]),
        case_id=UUID(props["case_id"]),
        evidence_id=UUID(props["evidence_id"]),
        observation_type=props["observation_type"],
        event_time=_to_datetime(props.get("event_time")),
        time_window_start=_to_datetime(props.get("time_window_start")),
        time_window_end=_to_datetime(props.get("time_window_end")),
        location_raw_text=props.get("location_raw_text"),
        location_latitude=props.get("location_latitude"),
        location_longitude=props.get("location_longitude"),
        location_precision_meters=props.get("location_precision_meters"),
        extraction_confidence=props["extraction_confidence"],
        source_locator=locator,
        extractor_name=props["extractor_name"],
        extractor_version=props["extractor_version"],
        extractor_config_hash=props["extractor_config_hash"],
        extractor_model_version=props["extractor_model_version"],
    )


def _entity_node_from_props(props: Mapping[str, Any]) -> EntityNode:
    return EntityNode(
        entity_id=UUID(props["entity_id"]),
        case_id=UUID(props["case_id"]),
        entity_type=props["entity_type"],
        canonical_label=props["canonical_label"],
        aliases=tuple(props.get("aliases") or ()),
        stable_identifiers_json=props["stable_identifiers_json"],
        review_status=props["review_status"],
        created_from_observation_ids=tuple(
            UUID(i) for i in (props.get("created_from_observation_ids") or ())
        ),
    )


def _event_node_from_props(props: Mapping[str, Any]) -> EventNode:
    return EventNode(
        event_id=UUID(props["event_id"]),
        case_id=UUID(props["case_id"]),
        event_type=props["event_type"],
        event_time=_to_datetime(props.get("event_time")),
        time_window_start=_to_datetime(props.get("time_window_start")),
        time_window_end=_to_datetime(props.get("time_window_end")),
        review_status=props["review_status"],
        confidence=props["confidence"],
        evidence_refs=tuple(UUID(i) for i in (props.get("evidence_refs") or ())),
    )


_SUMMARY_COUNT_QUERIES: tuple[tuple[str, str], ...] = (
    ("evidence_count", "MATCH (n:Evidence {case_id: $case_id}) RETURN count(n) AS count"),
    ("observation_count", "MATCH (n:Observation {case_id: $case_id}) RETURN count(n) AS count"),
    ("entity_count", "MATCH (n:Entity {case_id: $case_id}) RETURN count(n) AS count"),
    ("event_count", "MATCH (n:Event {case_id: $case_id}) RETURN count(n) AS count"),
)


async def get_case_graph_summary(
    repository: Neo4jGraphRepository, case_id: UUID
) -> CaseGraphSummary:
    """Node counts by kind for one case; all-zero for a case with no graph data yet."""
    case_id = _ensure_case_id(case_id)
    counts: dict[str, int] = {}
    for key, query in _SUMMARY_COUNT_QUERIES:
        rows = await repository.read(query, {"case_id": str(case_id)})
        counts[key] = int(rows[0]["count"]) if rows else 0
    return CaseGraphSummary(case_id=case_id, **counts)


async def get_evidence_provenance(
    repository: Neo4jGraphRepository, case_id: UUID, evidence_id: UUID
) -> EvidenceProvenance:
    """The `Evidence` node and the (bounded) observations it yielded, for one case."""
    case_id = _ensure_case_id(case_id)
    query = (
        "MATCH (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
        "OPTIONAL MATCH (e)-[:YIELDED_OBSERVATION]->(o:Observation {case_id: $case_id}) "
        "WITH e, o ORDER BY o.observation_id "
        "WITH e, collect(o) AS observations "
        "RETURN e AS evidence, observations[0..$limit] AS page, size(observations) AS total"
    )
    rows = await repository.read(
        query,
        {"case_id": str(case_id), "evidence_id": str(evidence_id), "limit": MAX_COLLECTION_LIMIT},
    )
    if not rows:
        raise GraphNotFoundError("evidence not found in this case")
    row = rows[0]
    total = int(row["total"])
    page = row["page"]
    return EvidenceProvenance(
        evidence=_evidence_node_from_props(row["evidence"]),
        observations=tuple(_observation_node_from_props(p) for p in page),
        total_observations=total,
        truncated=total > len(page),
    )


async def get_observation_provenance(
    repository: Neo4jGraphRepository, case_id: UUID, observation_id: UUID
) -> ObservationProvenance:
    """The `Observation` node and the `Evidence` node it was yielded from, for one case."""
    case_id = _ensure_case_id(case_id)
    query = (
        "MATCH (o:Observation {case_id: $case_id, observation_id: $observation_id}) "
        "OPTIONAL MATCH (e:Evidence {case_id: $case_id})-[:YIELDED_OBSERVATION]->(o) "
        "RETURN o AS observation, e AS evidence"
    )
    rows = await repository.read(
        query, {"case_id": str(case_id), "observation_id": str(observation_id)}
    )
    if not rows:
        raise GraphNotFoundError("observation not found in this case")
    row = rows[0]
    evidence_props = row["evidence"]
    return ObservationProvenance(
        observation=_observation_node_from_props(row["observation"]),
        evidence=_evidence_node_from_props(evidence_props) if evidence_props else None,
    )


async def get_event_with_participants(
    repository: Neo4jGraphRepository, case_id: UUID, event_id: UUID
) -> EventWithParticipants:
    """The `Event` node and the (bounded) `Entity` participants linked to it, for one case."""
    case_id = _ensure_case_id(case_id)
    query = (
        "MATCH (v:Event {case_id: $case_id, event_id: $event_id}) "
        "OPTIONAL MATCH (v)-[:HAS_PARTICIPANT]->(n:Entity {case_id: $case_id}) "
        "WITH v, n ORDER BY n.entity_id "
        "WITH v, collect(n) AS participants "
        "RETURN v AS event, participants[0..$limit] AS page, size(participants) AS total"
    )
    rows = await repository.read(
        query,
        {"case_id": str(case_id), "event_id": str(event_id), "limit": MAX_COLLECTION_LIMIT},
    )
    if not rows:
        raise GraphNotFoundError("event not found in this case")
    row = rows[0]
    total = int(row["total"])
    page = row["page"]
    return EventWithParticipants(
        event=_event_node_from_props(row["event"]),
        participants=tuple(_entity_node_from_props(p) for p in page),
        total_participants=total,
        truncated=total > len(page),
    )


async def get_entity_events(
    repository: Neo4jGraphRepository,
    case_id: UUID,
    entity_id: UUID,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> EntityEventsPage:
    """One bounded, oldest-first page of `Event`s an entity participated in, for one case.

    `start_time`/`end_time` filter on `Event.event_time` only -- an event
    recorded with only a `time_window` (no single `event_time`) is included
    in an unfiltered call but will not match a range filter in this phase.
    """
    case_id = _ensure_case_id(case_id)
    limit = _ensure_limit(limit)
    offset = _ensure_offset(offset)

    exists_query = (
        "MATCH (n:Entity {case_id: $case_id, entity_id: $entity_id}) "
        "RETURN n.entity_id AS entity_id"
    )
    exists_rows = await repository.read(
        exists_query, {"case_id": str(case_id), "entity_id": str(entity_id)}
    )
    if not exists_rows:
        raise GraphNotFoundError("entity not found in this case")

    query = (
        "MATCH (n:Entity {case_id: $case_id, entity_id: $entity_id})"
        "<-[:HAS_PARTICIPANT]-(v:Event {case_id: $case_id}) "
        "WHERE ($start_time IS NULL OR v.event_time >= $start_time) "
        "AND ($end_time IS NULL OR v.event_time <= $end_time) "
        "RETURN v AS event "
        "ORDER BY v.event_time "
        "SKIP $offset LIMIT $fetch_limit"
    )
    rows = await repository.read(
        query,
        {
            "case_id": str(case_id),
            "entity_id": str(entity_id),
            "start_time": start_time,
            "end_time": end_time,
            "offset": offset,
            "fetch_limit": limit + 1,
        },
    )
    page_rows = rows[:limit]
    return EntityEventsPage(
        entity_id=entity_id,
        events=tuple(_event_node_from_props(r["event"]) for r in page_rows),
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )
