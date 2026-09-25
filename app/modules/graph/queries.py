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
    CaseObservationsPage,
    EntityEventsPage,
    EntityMentionNode,
    EntityNode,
    EventNode,
    EventWithParticipants,
    EvidenceNode,
    EvidenceProvenance,
    GraphRelationshipKind,
    ObservationNode,
    ObservationProvenance,
    ObservationWithMentions,
    SourceLocatorRef,
)
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.schemas import (
    GraphAnalyticsResponse,
    GraphCoParticipationMotif,
    GraphMotifsResponse,
    GraphPathNodeView,
    GraphPathRequest,
    GraphPathResponse,
    GraphSnapshotEntityView,
    GraphSnapshotEventView,
    GraphSnapshotRelationshipView,
    GraphSnapshotResponse,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_COLLECTION_LIMIT = 500

#: Gap-Closure WP-6 (G7 rest): default/max bounds for the new snapshot/
#: analytics/motifs endpoints -- same bounded-result-set rule as every
#: other query in this module.
DEFAULT_SNAPSHOT_NODE_LIMIT = 500
MAX_SNAPSHOT_NODE_LIMIT = 2000
DEFAULT_SNAPSHOT_RELATIONSHIP_LIMIT = 1000
MAX_SNAPSHOT_RELATIONSHIP_LIMIT = 4000
DEFAULT_MOTIF_LIMIT = 100
MAX_MOTIF_LIMIT = 500
#: A fixed, hardcoded upper bound baked into the shortest-path Cypher text
#: itself (never a parameter -- see this module's own "no value is ever
#: interpolated into a query string" rule). `GraphPathRequest.max_hops`
#: (validated `le=15`) is enforced by filtering the result afterward: a
#: path Neo4j finds within this fixed ceiling that is longer than the
#: caller's requested `max_hops` is reported as not found, never truncated
#: or silently returned anyway.
_PATH_SEARCH_CEILING_HOPS = 15


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


def _entity_mention_node_from_props(props: Mapping[str, Any], ordinal: int) -> EntityMentionNode:
    return EntityMentionNode(
        mention_id=UUID(props["mention_id"]),
        case_id=UUID(props["case_id"]),
        observation_id=UUID(props["observation_id"]),
        mention_type=props.get("mention_type"),
        display_label=props["display_label"],
        ordinal=ordinal,
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


async def list_case_observations(
    repository: Neo4jGraphRepository,
    case_id: UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> CaseObservationsPage:
    """One bounded, stable-ordered page of a case's `Observation`s, each with its mentions.

    The backing read view for `GET /api/v1/cases/{case_id}/graph/observations`
    -- returns only the safe projected graph view (allow-listed `Observation`
    properties plus evidence-local `EntityMention`s, ordinal-ordered); never
    an object URI, raw evidence body, or anything not already subject to
    `projection.py`'s own allow-lists. Ordered by `observation_id` (stable,
    not insertion-order-dependent) so repeated pagination never skips or
    duplicates a row even if new observations are projected between pages.
    """
    case_id = _ensure_case_id(case_id)
    limit = _ensure_limit(limit)
    offset = _ensure_offset(offset)

    page_query = (
        "MATCH (o:Observation {case_id: $case_id}) "
        "RETURN o AS observation "
        "ORDER BY o.observation_id "
        "SKIP $offset LIMIT $fetch_limit"
    )
    rows = await repository.read(
        page_query, {"case_id": str(case_id), "offset": offset, "fetch_limit": limit + 1}
    )
    page_rows = rows[:limit]
    has_more = len(rows) > limit

    observation_ids = [row["observation"]["observation_id"] for row in page_rows]
    mentions_by_observation: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    if observation_ids:
        mentions_query = (
            "MATCH (o:Observation {case_id: $case_id})"
            "-[r:MENTIONS]->(m:EntityMention {case_id: $case_id}) "
            "WHERE o.observation_id IN $observation_ids "
            "RETURN o.observation_id AS observation_id, m AS mention, r.ordinal AS ordinal "
            "ORDER BY o.observation_id, r.ordinal"
        )
        mention_rows = await repository.read(
            mentions_query, {"case_id": str(case_id), "observation_ids": observation_ids}
        )
        for mention_row in mention_rows:
            mentions_by_observation.setdefault(mention_row["observation_id"], []).append(
                (mention_row["ordinal"], mention_row["mention"])
            )

    items = tuple(
        ObservationWithMentions(
            observation=_observation_node_from_props(row["observation"]),
            mentions=tuple(
                _entity_mention_node_from_props(props, ordinal)
                for ordinal, props in mentions_by_observation.get(
                    row["observation"]["observation_id"], []
                )
            ),
        )
        for row in page_rows
    )
    return CaseObservationsPage(
        case_id=case_id, items=items, limit=limit, offset=offset, has_more=has_more
    )


# --- Gap-Closure WP-6 (G7 rest): case graph snapshot/path/analytics/motifs -----


def _ensure_bounded(value: int, *, maximum: int, name: str) -> int:
    if value <= 0 or value > maximum:
        raise GraphValidationError(f"{name} must be between 1 and {maximum}")
    return value


async def get_case_graph_snapshot(
    repository: Neo4jGraphRepository,
    case_id: UUID,
    *,
    node_limit: int = DEFAULT_SNAPSHOT_NODE_LIMIT,
    relationship_limit: int = DEFAULT_SNAPSHOT_RELATIONSHIP_LIMIT,
) -> GraphSnapshotResponse:
    """A bounded `Entity`/`Event` snapshot plus their connecting relationships.

    Scoped to the two node kinds an analyst's graph view needs (see
    `schemas.py`'s module-level comment for why the other node kinds are
    excluded). Each of the three relationship kinds is fetched with its
    own independent `relationship_limit` -- a case with many
    `HAS_PARTICIPANT` edges never starves the smaller, equally important
    `POSSIBLY_SAME_AS`/`CONTRADICTED_BY` result sets.
    """
    case_id = _ensure_case_id(case_id)
    node_limit = _ensure_bounded(node_limit, maximum=MAX_SNAPSHOT_NODE_LIMIT, name="node_limit")
    relationship_limit = _ensure_bounded(
        relationship_limit, maximum=MAX_SNAPSHOT_RELATIONSHIP_LIMIT, name="relationship_limit"
    )
    params = {"case_id": str(case_id)}

    entity_rows = await repository.read(
        "MATCH (e:Entity {case_id: $case_id}) "
        "RETURN e.entity_id AS entity_id, e.entity_type AS entity_type, "
        "e.canonical_label AS canonical_label, e.aliases AS aliases, "
        "e.review_status AS review_status "
        "LIMIT $limit",
        {**params, "limit": node_limit},
    )
    event_rows = await repository.read(
        "MATCH (v:Event {case_id: $case_id}) "
        "RETURN v.event_id AS event_id, v.event_type AS event_type, "
        "v.review_status AS review_status, v.confidence AS confidence, "
        "v.event_time AS event_time "
        "LIMIT $limit",
        {**params, "limit": node_limit},
    )
    relationships: list[GraphSnapshotRelationshipView] = []
    for kind, query in (
        (
            GraphRelationshipKind.HAS_PARTICIPANT,
            "MATCH (v:Event {case_id: $case_id})"
            "-[:HAS_PARTICIPANT]->(e:Entity {case_id: $case_id}) "
            "RETURN v.event_id AS from_id, e.entity_id AS to_id, "
            "null AS relationship_id LIMIT $limit",
        ),
        (
            GraphRelationshipKind.POSSIBLY_SAME_AS,
            "MATCH (a:Entity {case_id: $case_id})"
            "-[rel:POSSIBLY_SAME_AS]->(b:Entity {case_id: $case_id}) "
            "RETURN a.entity_id AS from_id, b.entity_id AS to_id, "
            "rel.entity_resolution_candidate_id AS relationship_id LIMIT $limit",
        ),
        (
            GraphRelationshipKind.CONTRADICTED_BY,
            "MATCH (a:Entity {case_id: $case_id})"
            "-[rel:CONTRADICTED_BY]->(b:Entity {case_id: $case_id}) "
            "RETURN a.entity_id AS from_id, b.entity_id AS to_id, "
            "rel.entity_resolution_candidate_id AS relationship_id LIMIT $limit",
        ),
    ):
        rows = await repository.read(query, {**params, "limit": relationship_limit})
        relationships.extend(
            GraphSnapshotRelationshipView(
                kind=kind,
                from_id=row["from_id"],
                to_id=row["to_id"],
                relationship_id=row["relationship_id"],
            )
            for row in rows
        )

    return GraphSnapshotResponse(
        case_id=case_id,
        entities=tuple(
            GraphSnapshotEntityView(
                entity_id=row["entity_id"],
                entity_type=row["entity_type"],
                canonical_label=row["canonical_label"],
                aliases=tuple(row["aliases"] or ()),
                review_status=row["review_status"],
            )
            for row in entity_rows
        ),
        events=tuple(
            GraphSnapshotEventView(
                event_id=row["event_id"],
                event_type=row["event_type"],
                review_status=row["review_status"],
                confidence=row["confidence"],
                event_time=_to_datetime(row.get("event_time")),
            )
            for row in event_rows
        ),
        relationships=tuple(relationships),
        node_limit=node_limit,
        relationship_limit=relationship_limit,
    )


def _path_endpoint_clause(
    variable: str, *, entity_id: UUID | None, event_id: UUID | None
) -> tuple[str, dict[str, str]]:
    if entity_id is not None:
        return (
            f"({variable}:Entity {{case_id: $case_id, entity_id: ${variable}_id}})",
            {f"{variable}_id": str(entity_id)},
        )
    if event_id is None:  # pragma: no cover - GraphPathRequest's own validator prevents this
        raise GraphValidationError(f"{variable}: exactly one of entity_id/event_id is required")
    return (
        f"({variable}:Event {{case_id: $case_id, event_id: ${variable}_id}})",
        {f"{variable}_id": str(event_id)},
    )


async def find_case_graph_path(
    repository: Neo4jGraphRepository, case_id: UUID, request: GraphPathRequest
) -> GraphPathResponse:
    """Shortest path between one `Entity`/`Event` and another, both case-scoped.

    Always searches up to a fixed `_PATH_SEARCH_CEILING_HOPS` (a literal in
    the query text, never a parameter); `request.max_hops` is enforced by
    filtering the result afterward, never by interpolating a value into
    the query -- see this module's own rule against that.
    """
    case_id = _ensure_case_id(case_id)
    from_clause, from_params = _path_endpoint_clause(
        "from_node", entity_id=request.from_entity_id, event_id=request.from_event_id
    )
    to_clause, to_params = _path_endpoint_clause(
        "to_node", entity_id=request.to_entity_id, event_id=request.to_event_id
    )
    query = (
        f"MATCH {from_clause} "
        f"MATCH {to_clause} "
        f"MATCH p = shortestPath((from_node)-[*..{_PATH_SEARCH_CEILING_HOPS}]-(to_node)) "
        "RETURN [n IN nodes(p) | {labels: labels(n), "
        "id: coalesce(n.entity_id, n.event_id)}] AS path_nodes, "
        "[r IN relationships(p) | {kind: type(r), "
        "from_id: coalesce(startNode(r).entity_id, startNode(r).event_id), "
        "to_id: coalesce(endNode(r).entity_id, endNode(r).event_id)}] AS path_relationships, "
        "length(p) AS path_length"
    )
    rows = await repository.read(query, {"case_id": str(case_id), **from_params, **to_params})
    if not rows or rows[0]["path_length"] > request.max_hops:
        return GraphPathResponse(case_id=case_id, found=False, nodes=(), relationships=())

    row = rows[0]
    return GraphPathResponse(
        case_id=case_id,
        found=True,
        nodes=tuple(
            GraphPathNodeView(kind=node["labels"][0], node_id=node["id"])
            for node in row["path_nodes"]
        ),
        relationships=tuple(
            GraphSnapshotRelationshipView(
                kind=GraphRelationshipKind(rel["kind"]), from_id=rel["from_id"], to_id=rel["to_id"]
            )
            for rel in row["path_relationships"]
        ),
    )


async def get_case_graph_analytics(
    repository: Neo4jGraphRepository, case_id: UUID
) -> GraphAnalyticsResponse:
    """Aggregate node/relationship counts only -- no property values, so
    every node/relationship kind is safe to include here regardless of
    what the snapshot endpoint scopes down to."""
    case_id = _ensure_case_id(case_id)
    params = {"case_id": str(case_id)}
    node_rows = await repository.read(
        "MATCH (n {case_id: $case_id}) RETURN labels(n)[0] AS label, count(n) AS node_count",
        params,
    )
    relationship_rows = await repository.read(
        "MATCH (a {case_id: $case_id})-[r]->(b {case_id: $case_id}) "
        "RETURN type(r) AS kind, count(r) AS relationship_count",
        params,
    )
    return GraphAnalyticsResponse(
        case_id=case_id,
        node_counts={row["label"]: row["node_count"] for row in node_rows if row["label"]},
        relationship_counts={row["kind"]: row["relationship_count"] for row in relationship_rows},
    )


async def get_case_graph_motifs(
    repository: Neo4jGraphRepository, case_id: UUID, *, limit: int = DEFAULT_MOTIF_LIMIT
) -> GraphMotifsResponse:
    """Generic co-participation motifs: pairs of `Event`s sharing a common
    `Entity` participant. See `GraphCoParticipationMotif`'s docstring for
    why this is deliberately generic, not a scenario-specific pattern."""
    case_id = _ensure_case_id(case_id)
    limit = _ensure_bounded(limit, maximum=MAX_MOTIF_LIMIT, name="limit")
    rows = await repository.read(
        "MATCH (e1:Event {case_id: $case_id})-[:HAS_PARTICIPANT]->"
        "(entity:Entity {case_id: $case_id})<-[:HAS_PARTICIPANT]-(e2:Event {case_id: $case_id}) "
        "WHERE e1.event_id < e2.event_id "
        "RETURN entity.entity_id AS shared_entity_id, e1.event_id AS event_a_id, "
        "e2.event_id AS event_b_id "
        "LIMIT $limit",
        {"case_id": str(case_id), "limit": limit},
    )
    return GraphMotifsResponse(
        case_id=case_id,
        co_participation=tuple(
            GraphCoParticipationMotif(
                shared_entity_id=row["shared_entity_id"],
                event_a_id=row["event_a_id"],
                event_b_id=row["event_b_id"],
            )
            for row in rows
        ),
        limit=limit,
    )
