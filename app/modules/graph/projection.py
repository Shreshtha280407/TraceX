"""Idempotent projection of canonical contracts into the Neo4j graph.

## Allow-listed properties only

No node written by this module ever carries a raw evidence payload, whole
document text, full transcript, raw video frame, or an unfiltered
`attributes`/`stable_identifiers` blob. Each `_*_properties` function below
is the exhaustive allow-list for its node type -- the `*_ALLOWED_PROPERTIES`
constants exist specifically so `tests/unit/graph/` can assert a built
property map never contains an unlisted key. Two deliberate omissions from
every node type: `ObservationV1.attributes`, `EntityV1.attributes`, and
`EventV1.attributes` (open-ended `dict[str, JsonValue]` bags that could hold
arbitrarily large or sensitive extractor-specific data) are never projected.
`EntityV1.stable_identifiers` is the one open-ended field that *is*
projected, because entity-type-specific identifiers (phone/PAN/account
numbers) are exactly what future entity-resolution/correlation phases need
to read back -- it is serialized through `app.core.canonical.canonical_bytes`
into a single deterministic JSON string property (`stable_identifiers_json`),
since Neo4j node properties cannot hold nested maps. Likewise,
`ExtractedEntityMention.attributes` is never projected onto `EntityMention`.

## `EntityMention`: evidence-local, never a resolved entity

`project_observation_mentions` projects each of `ObservationV1.
extracted_entities` as its own `EntityMention` node, linked from its parent
`Observation` via `MENTIONS {ordinal}` (`ordinal` is the mention's index in
that list, preserved so a re-read can reconstruct the original order).
Unlike `project_entity`, this function is never handed anything to merge
identity against -- it has no `entity_id`, no candidate-matching, and never
looks at another observation's mentions. `mention_id` is derived
deterministically from `(case_id, observation_id, ordinal, normalized text)`
via `app.core.ids.deterministic_uuid`, so re-projecting the same canonical
observation is idempotent, but two different observations that happen to
mention the same-looking text always get two distinct `EntityMention`
nodes -- there is no cross-observation dedup or fuzzy matching here at all.
That resolution step (if it ever happens) is later-phase entity-resolution
work, entirely outside this module.

## Case isolation

Every node is identified by a compound `(case_id, <domain>_id)` key in every
`MERGE` -- never by the domain ID alone -- so a second case reusing the same
`evidence_id`/`entity_id`/etc. (or the same-looking external identifier,
e.g. two cases both citing phone number `+911234567890`) can never share
graph identity with the first case's node. `schema.py` backs this with a
database-level composite uniqueness constraint per label.

## Dependency ordering: MERGE only what already exists as a real node

`project_observation` requires its `evidence_id` to already be a projected
`Evidence` node in the same case; `project_event` requires every
`participant_entity_ids` entry to already be a projected `Entity` node in
the same case. Neither creates a stub/fake node to satisfy a relationship
target: if a dependency is missing, the call is a no-op against the graph
and returns a `DEFERRED` result naming exactly what's missing, so a caller
can re-project once the dependency exists. `project_evidence` and
`project_entity` have no such dependency (nothing in `EvidenceRecordV1` or
`EntityV1` references another graph node), so they always apply.

`EventV1.participant_entity_ids`/`evidence_refs` and `EntityV1.
created_from_observation_ids` are stored as plain ID-array properties, not
as graph relationships -- `docs/architecture/graph-taxonomy-v1.md` does not
define an `Entity->Observation` or `Event->Evidence` edge type in this
phase, and a plain property array carries no risk of pointing a real
relationship at a node that doesn't exist.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from app.contracts.entity import EntityV1
from app.contracts.event import EventV1
from app.contracts.evidence import EvidenceRecordV1
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.core.canonical import canonical_bytes
from app.core.ids import deterministic_uuid
from app.modules.graph.mapping import GraphProjectionPlan, MappingStatus
from app.modules.graph.models import (
    EntityMentionProjectionResult,
    EventProjectionResult,
    GraphNodeKind,
    ObservationProjectionResult,
    ProjectionOutcome,
    ProjectionResult,
)
from app.modules.graph.repository import GraphRecord, Neo4jGraphRepository

EVIDENCE_ALLOWED_PROPERTIES = frozenset(
    {
        "evidence_id",
        "case_id",
        "source_type",
        "content_type",
        "object_uri",
        "sha256",
        "classification",
        "processing_status",
    }
)

OBSERVATION_ALLOWED_PROPERTIES = frozenset(
    {
        "observation_id",
        "case_id",
        "evidence_id",
        "observation_type",
        "extraction_confidence",
        "extractor_name",
        "extractor_version",
        "extractor_config_hash",
        "extractor_model_version",
        "event_time",
        "time_window_start",
        "time_window_end",
        "location_raw_text",
        "location_latitude",
        "location_longitude",
        "location_precision_meters",
        "source_locator_page",
        "source_locator_span_start",
        "source_locator_span_end",
        "source_locator_bbox_x_min",
        "source_locator_bbox_y_min",
        "source_locator_bbox_x_max",
        "source_locator_bbox_y_max",
        "source_locator_sheet",
        "source_locator_row",
        "source_locator_column",
        "source_locator_json_path",
        "source_locator_frame_number",
        "source_locator_time_start_ms",
        "source_locator_time_end_ms",
        "source_locator_message_id",
        "mapping_status",
        "mapping_reason",
        "mapping_version",
        "mapping_config_hash",
    }
)

#: Namespace label so an `EntityMention`'s deterministic ID can never
#: collide with a different domain's `deterministic_uuid` call using the
#: same raw parts by coincidence.
_ENTITY_MENTION_ID_NAMESPACE_LABEL = "entity_mention"

_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")

ENTITY_MENTION_ALLOWED_PROPERTIES = frozenset(
    {
        "mention_id",
        "case_id",
        "observation_id",
        "mention_type",
        "display_label",
    }
)

ENTITY_ALLOWED_PROPERTIES = frozenset(
    {
        "entity_id",
        "case_id",
        "entity_type",
        "canonical_label",
        "aliases",
        "stable_identifiers_json",
        "review_status",
        "created_from_observation_ids",
    }
)

EVENT_ALLOWED_PROPERTIES = frozenset(
    {
        "event_id",
        "case_id",
        "event_type",
        "review_status",
        "confidence",
        "evidence_refs",
        "event_time",
        "time_window_start",
        "time_window_end",
    }
)


def _evidence_properties(record: EvidenceRecordV1) -> dict[str, Any]:
    return {
        "evidence_id": str(record.evidence_id),
        "case_id": str(record.case_id),
        "source_type": record.source_type.value,
        "content_type": record.content_type,
        "object_uri": record.object_uri,
        "sha256": record.sha256,
        "classification": record.classification.value,
        "processing_status": record.processing_status.value,
    }


def _observation_properties(
    observation: ObservationV1, mapping_plan: GraphProjectionPlan | None = None
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "observation_id": str(observation.observation_id),
        "case_id": str(observation.case_id),
        "evidence_id": str(observation.evidence_id),
        "observation_type": observation.observation_type,
        "extraction_confidence": observation.extraction_confidence,
        "extractor_name": observation.extractor.name,
        "extractor_version": observation.extractor.version,
        "extractor_config_hash": observation.extractor.config_hash,
        "extractor_model_version": observation.extractor.model_version,
    }
    if observation.event_time is not None:
        props["event_time"] = observation.event_time
    if observation.time_window is not None:
        if observation.time_window.start is not None:
            props["time_window_start"] = observation.time_window.start
        if observation.time_window.end is not None:
            props["time_window_end"] = observation.time_window.end
    if observation.location is not None:
        location = observation.location
        if location.raw_text is not None:
            props["location_raw_text"] = location.raw_text
        if location.latitude is not None:
            props["location_latitude"] = location.latitude
        if location.longitude is not None:
            props["location_longitude"] = location.longitude
        if location.precision_meters is not None:
            props["location_precision_meters"] = location.precision_meters
    locator = observation.source_locator
    if locator.page is not None:
        props["source_locator_page"] = locator.page
    if locator.span_start is not None:
        props["source_locator_span_start"] = locator.span_start
    if locator.span_end is not None:
        props["source_locator_span_end"] = locator.span_end
    if locator.bbox_xyxy_normalized is not None:
        bbox = locator.bbox_xyxy_normalized
        props["source_locator_bbox_x_min"] = bbox.x_min
        props["source_locator_bbox_y_min"] = bbox.y_min
        props["source_locator_bbox_x_max"] = bbox.x_max
        props["source_locator_bbox_y_max"] = bbox.y_max
    if locator.sheet is not None:
        props["source_locator_sheet"] = locator.sheet
    if locator.row is not None:
        props["source_locator_row"] = locator.row
    if locator.column is not None:
        props["source_locator_column"] = locator.column
    if locator.json_path is not None:
        props["source_locator_json_path"] = locator.json_path
    if locator.frame_number is not None:
        props["source_locator_frame_number"] = locator.frame_number
    if locator.time_start_ms is not None:
        props["source_locator_time_start_ms"] = locator.time_start_ms
    if locator.time_end_ms is not None:
        props["source_locator_time_end_ms"] = locator.time_end_ms
    if locator.message_id is not None:
        props["source_locator_message_id"] = locator.message_id
    if mapping_plan is not None:
        props["mapping_status"] = mapping_plan.status.value
        props["mapping_reason"] = mapping_plan.reason
        props["mapping_version"] = mapping_plan.mapping_version
        props["mapping_config_hash"] = mapping_plan.mapping_config_hash
    return props


def _entity_properties(entity: EntityV1) -> dict[str, Any]:
    stable_identifiers_json = canonical_bytes(dict(entity.stable_identifiers)).decode("utf-8")
    return {
        "entity_id": str(entity.entity_id),
        "case_id": str(entity.case_id),
        "entity_type": entity.entity_type,
        "canonical_label": entity.canonical_label,
        "aliases": list(entity.aliases),
        "stable_identifiers_json": stable_identifiers_json,
        "review_status": entity.review_status.value,
        "created_from_observation_ids": [str(i) for i in entity.created_from_observation_ids],
    }


def _event_properties(event: EventV1) -> dict[str, Any]:
    props: dict[str, Any] = {
        "event_id": str(event.event_id),
        "case_id": str(event.case_id),
        "event_type": event.event_type,
        "review_status": event.review_status.value,
        "confidence": event.confidence,
        "evidence_refs": [str(i) for i in event.evidence_refs],
    }
    if event.event_time is not None:
        props["event_time"] = event.event_time
    if event.time_window is not None:
        if event.time_window.start is not None:
            props["time_window_start"] = event.time_window.start
        if event.time_window.end is not None:
            props["time_window_end"] = event.time_window.end
    return props


def _build_evidence_merge_query(record: EvidenceRecordV1) -> tuple[str, dict[str, Any]]:
    query = (
        "MERGE (c:Case {case_id: $case_id}) "
        "MERGE (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
        "SET e += $properties "
        "MERGE (c)-[:HAS_EVIDENCE]->(e) "
        "RETURN e.evidence_id AS evidence_id"
    )
    params = {
        "case_id": str(record.case_id),
        "evidence_id": str(record.evidence_id),
        "properties": _evidence_properties(record),
    }
    return query, params


def _build_observation_merge_query(
    observation: ObservationV1, mapping_plan: GraphProjectionPlan | None = None
) -> tuple[str, dict[str, Any]]:
    # The leading MATCH on Evidence makes this query an atomic no-op when the
    # evidence hasn't been projected yet: if it matches nothing, every clause
    # after it (including both MERGEs) executes zero times, so no Case,
    # Observation, or relationship is written -- see `project_observation`.
    query = (
        "MATCH (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
        "MERGE (c:Case {case_id: $case_id}) "
        "MERGE (o:Observation {case_id: $case_id, observation_id: $observation_id}) "
        "SET o += $properties "
        "MERGE (c)-[:HAS_OBSERVATION]->(o) "
        "MERGE (e)-[:YIELDED_OBSERVATION]->(o) "
        "RETURN o.observation_id AS observation_id"
    )
    params = {
        "case_id": str(observation.case_id),
        "evidence_id": str(observation.evidence_id),
        "observation_id": str(observation.observation_id),
        "properties": _observation_properties(observation, mapping_plan),
    }
    return query, params


def _build_entity_merge_query(entity: EntityV1) -> tuple[str, dict[str, Any]]:
    query = (
        "MERGE (c:Case {case_id: $case_id}) "
        "MERGE (n:Entity {case_id: $case_id, entity_id: $entity_id}) "
        "SET n += $properties "
        "MERGE (c)-[:HAS_ENTITY]->(n) "
        "RETURN n.entity_id AS entity_id"
    )
    params = {
        "case_id": str(entity.case_id),
        "entity_id": str(entity.entity_id),
        "properties": _entity_properties(entity),
    }
    return query, params


def _build_event_merge_query(event: EventV1) -> tuple[str, dict[str, Any]]:
    # Same atomic-no-op shape as observation, generalized to N dependencies:
    # the WHERE clause only lets execution continue into the MERGEs when
    # every requested participant matched a real Entity node. `EventV1.
    # participant_entity_ids` has `min_length=1`, so `requested_count` is
    # never 0 -- a 0-participant event can't reach this code at all.
    query = (
        "UNWIND $participant_entity_ids AS participant_id "
        "OPTIONAL MATCH (n:Entity {case_id: $case_id, entity_id: participant_id}) "
        "WITH collect(n) AS matched_nodes, count(n) AS matched_count, "
        "count(participant_id) AS requested_count "
        "WHERE matched_count = requested_count "
        "MERGE (c:Case {case_id: $case_id}) "
        "MERGE (v:Event {case_id: $case_id, event_id: $event_id}) "
        "SET v += $properties "
        "MERGE (c)-[:HAS_EVENT]->(v) "
        "WITH v, matched_nodes "
        "UNWIND matched_nodes AS n "
        "MERGE (v)-[:HAS_PARTICIPANT]->(n) "
        "RETURN DISTINCT v.event_id AS event_id"
    )
    params = {
        "case_id": str(event.case_id),
        "event_id": str(event.event_id),
        "participant_entity_ids": [str(i) for i in event.participant_entity_ids],
        "properties": _event_properties(event),
    }
    return query, params


def _build_missing_entities_query(
    case_id: UUID, entity_ids: Sequence[UUID]
) -> tuple[str, dict[str, Any]]:
    query = (
        "UNWIND $entity_ids AS entity_id "
        "OPTIONAL MATCH (n:Entity {case_id: $case_id, entity_id: entity_id}) "
        "WITH entity_id, n WHERE n IS NULL "
        "RETURN DISTINCT entity_id"
    )
    params = {"case_id": str(case_id), "entity_ids": [str(i) for i in entity_ids]}
    return query, params


async def _find_missing_entities(
    repository: Neo4jGraphRepository, case_id: UUID, entity_ids: Sequence[UUID]
) -> list[UUID]:
    query, params = _build_missing_entities_query(case_id, entity_ids)
    rows: list[GraphRecord] = await repository.read(query, params)
    return [UUID(row["entity_id"]) for row in rows]


async def project_evidence(
    repository: Neo4jGraphRepository, record: EvidenceRecordV1
) -> ProjectionResult:
    """Idempotently MERGE an `EvidenceRecordV1` into the graph.

    Has no dependency on another node type, so this always applies.
    """
    query, params = _build_evidence_merge_query(record)
    await repository.write(query, params)
    return ProjectionResult(
        outcome=ProjectionOutcome.APPLIED,
        node_kind=GraphNodeKind.EVIDENCE,
        case_id=record.case_id,
        domain_id=record.evidence_id,
        relationships_upserted=1,
    )


async def project_observation(
    repository: Neo4jGraphRepository,
    observation: ObservationV1,
    mapping_plan: GraphProjectionPlan | None = None,
) -> ObservationProjectionResult:
    """Idempotently MERGE an `ObservationV1` into the graph.

    Defers (writes nothing) if `observation.evidence_id` has not been
    projected as an `Evidence` node in this case yet.
    """
    query, params = _build_observation_merge_query(observation, mapping_plan)
    rows = await repository.write(query, params)
    if rows:
        return ObservationProjectionResult(
            outcome=ProjectionOutcome.APPLIED,
            node_kind=GraphNodeKind.OBSERVATION,
            case_id=observation.case_id,
            domain_id=observation.observation_id,
            relationships_upserted=2,
        )
    return ObservationProjectionResult(
        outcome=ProjectionOutcome.DEFERRED,
        node_kind=GraphNodeKind.OBSERVATION,
        case_id=observation.case_id,
        domain_id=observation.observation_id,
        relationships_upserted=0,
        missing_evidence_id=observation.evidence_id,
    )


def _normalize_mention_text(text: str) -> str:
    """Stable, content-preserving normalization for mention-ID derivation only.

    Unicode NFC + collapsed/stripped whitespace -- exactly the same
    normalization class `deterministic_uuid`'s own docstring asks callers to
    apply, and nothing more: no casefolding, no fuzzy matching. Two mentions
    differing only in case are still different content and get different
    IDs; this only makes byte-identical-looking text that differs solely in
    whitespace/composition form hash identically, so re-projecting the same
    canonical observation is always stable regardless of how a client's
    JSON encoder happened to serialize whitespace.
    """
    return _WHITESPACE_RUN_PATTERN.sub(" ", unicodedata.normalize("NFC", text)).strip()


def _entity_mention_id(case_id: UUID, observation_id: UUID, ordinal: int, text: str) -> UUID:
    """Deterministic from case+observation identity, ordinal, and normalized text.

    Never from `entity_type_hint` alone or from text similarity -- two
    mentions are the same node only if they are the exact same position in
    the exact same observation, so re-projecting a canonical observation
    (whose `extracted_entities` list is immutable once persisted) always
    yields the same mention IDs, and two different observations mentioning
    the same-looking text always get different, evidence-local mentions.
    """
    return deterministic_uuid(
        _ENTITY_MENTION_ID_NAMESPACE_LABEL,
        str(case_id),
        str(observation_id),
        str(ordinal),
        _normalize_mention_text(text),
    )


def _entity_mention_properties(
    case_id: UUID, observation_id: UUID, mention_id: UUID, mention: ExtractedEntityMention
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "mention_id": str(mention_id),
        "case_id": str(case_id),
        "observation_id": str(observation_id),
        "display_label": mention.text,
    }
    if mention.entity_type_hint is not None:
        props["mention_type"] = mention.entity_type_hint
    return props


def _build_one_mention_param(
    observation: ObservationV1, ordinal: int, mention: ExtractedEntityMention
) -> dict[str, Any]:
    mention_id = _entity_mention_id(
        observation.case_id, observation.observation_id, ordinal, mention.text
    )
    return {
        "mention_id": str(mention_id),
        "ordinal": ordinal,
        "properties": _entity_mention_properties(
            observation.case_id, observation.observation_id, mention_id, mention
        ),
    }


def _build_entity_mention_merge_query(observation: ObservationV1) -> tuple[str, dict[str, Any]]:
    # Same atomic-no-op shape as `_build_observation_merge_query`: the
    # leading MATCH on Observation makes this an all-or-nothing no-op when
    # the observation hasn't been projected yet. Only called when
    # `extracted_entities` is non-empty -- see `project_observation_mentions`.
    query = (
        "MATCH (o:Observation {case_id: $case_id, observation_id: $observation_id}) "
        "UNWIND $mentions AS mention "
        "MERGE (m:EntityMention {case_id: $case_id, mention_id: mention.mention_id}) "
        "SET m += mention.properties "
        "MERGE (o)-[r:MENTIONS]->(m) "
        "SET r.ordinal = mention.ordinal "
        "RETURN count(m) AS mention_count"
    )
    mentions = [
        _build_one_mention_param(observation, ordinal, mention)
        for ordinal, mention in enumerate(observation.extracted_entities)
    ]
    params = {
        "case_id": str(observation.case_id),
        "observation_id": str(observation.observation_id),
        "mentions": mentions,
    }
    return query, params


async def project_observation_mentions(
    repository: Neo4jGraphRepository, observation: ObservationV1
) -> EntityMentionProjectionResult:
    """Idempotently MERGE every `ObservationV1.extracted_entities` entry as an `EntityMention`.

    Each mention is evidence-local only -- never an `EntityV1`, never
    merged with another mention across observations, never inferred to be
    the same real-world entity as anything else. Defers (writes nothing) if
    `observation` has not itself been projected as an `Observation` node in
    this case yet -- callers should `project_observation` first, which is
    exactly the order `app.modules.graph.projector` always uses. An
    observation with no `extracted_entities` is trivially `APPLIED` with
    `mention_count=0` -- there is nothing to write, so no query even runs.
    """
    if not observation.extracted_entities:
        return EntityMentionProjectionResult(
            outcome=ProjectionOutcome.APPLIED,
            case_id=observation.case_id,
            observation_id=observation.observation_id,
            mention_count=0,
        )
    query, params = _build_entity_mention_merge_query(observation)
    rows = await repository.write(query, params)
    if rows:
        return EntityMentionProjectionResult(
            outcome=ProjectionOutcome.APPLIED,
            case_id=observation.case_id,
            observation_id=observation.observation_id,
            mention_count=len(observation.extracted_entities),
        )
    return EntityMentionProjectionResult(
        outcome=ProjectionOutcome.DEFERRED,
        case_id=observation.case_id,
        observation_id=observation.observation_id,
        mention_count=0,
        missing_observation_id=observation.observation_id,
    )


async def project_entity(repository: Neo4jGraphRepository, entity: EntityV1) -> ProjectionResult:
    """Idempotently MERGE an `EntityV1` into the graph.

    Has no dependency on another node type, so this always applies. This is
    a direct canonical projection only -- it never merges two `Entity`
    nodes together (see `docs/architecture/graph-taxonomy-v1.md`, "Identity
    and relationship safety").
    """
    query, params = _build_entity_merge_query(entity)
    await repository.write(query, params)
    return ProjectionResult(
        outcome=ProjectionOutcome.APPLIED,
        node_kind=GraphNodeKind.ENTITY,
        case_id=entity.case_id,
        domain_id=entity.entity_id,
        relationships_upserted=1,
    )


async def project_event(repository: Neo4jGraphRepository, event: EventV1) -> EventProjectionResult:
    """Idempotently MERGE an `EventV1` into the graph.

    Defers -- writing nothing at all, including the `Event` node itself --
    if any `participant_entity_ids` entry has not been projected as an
    `Entity` node in this case yet. This is intentionally all-or-nothing per
    event: a queryable `Event` never has a partial `HAS_PARTICIPANT` list.
    """
    query, params = _build_event_merge_query(event)
    rows = await repository.write(query, params)
    if rows:
        return EventProjectionResult(
            outcome=ProjectionOutcome.APPLIED,
            node_kind=GraphNodeKind.EVENT,
            case_id=event.case_id,
            domain_id=event.event_id,
            relationships_upserted=1 + len(set(event.participant_entity_ids)),
        )
    missing = await _find_missing_entities(repository, event.case_id, event.participant_entity_ids)
    return EventProjectionResult(
        outcome=ProjectionOutcome.DEFERRED,
        node_kind=GraphNodeKind.EVENT,
        case_id=event.case_id,
        domain_id=event.event_id,
        relationships_upserted=0,
        missing_participant_entity_ids=tuple(missing),
    )


def _mapping_claim_properties(
    observation: ObservationV1, plan: GraphProjectionPlan, claim: Any
) -> dict[str, Any]:
    """The bounded properties for a source claim; never project attributes bags."""
    return {
        "claim_id": str(claim.claim_id),
        "case_id": str(observation.case_id),
        "evidence_id": str(observation.evidence_id),
        "observation_id": str(observation.observation_id),
        "source_observation_type": observation.observation_type,
        "claim_type": claim.claim_type,
        "display_label": claim.display_label,
        "extraction_confidence": observation.extraction_confidence,
        "extractor_name": observation.extractor.name,
        "extractor_version": observation.extractor.version,
        "extractor_config_hash": observation.extractor.config_hash,
        "extractor_model_version": observation.extractor.model_version,
        "mapping_version": plan.mapping_version,
        "mapping_config_hash": plan.mapping_config_hash,
    }


def _specialized_locator_properties(observation: ObservationV1) -> dict[str, Any]:
    """Duplicate the bounded source locator onto a specialised temporal event.

    The parent observation remains authoritative, but retaining this compact
    allow-list makes an event independently reviewable without copying its
    open-ended attribute bag or source content.
    """
    locator = observation.source_locator
    props: dict[str, Any] = {}
    for field, value in (
        ("source_locator_json_path", locator.json_path),
        ("source_locator_frame_number", locator.frame_number),
        ("source_locator_time_start_ms", locator.time_start_ms),
        ("source_locator_time_end_ms", locator.time_end_ms),
        ("source_locator_message_id", locator.message_id),
    ):
        if value is not None:
            props[field] = value
    if locator.bbox_xyxy_normalized is not None:
        bbox = locator.bbox_xyxy_normalized
        props.update(
            {
                "source_locator_bbox_x_min": bbox.x_min,
                "source_locator_bbox_y_min": bbox.y_min,
                "source_locator_bbox_x_max": bbox.x_max,
                "source_locator_bbox_y_max": bbox.y_max,
            }
        )
    return props


async def project_specialized_mapping(
    repository: Neo4jGraphRepository, observation: ObservationV1, plan: GraphProjectionPlan
) -> ProjectionResult | None:
    """Apply a pure mapping plan after its canonical observation is present.

    An unsupported/deferred plan creates no specialised nodes, but its stable
    outcome is already stored on the parent ``Observation`` by
    ``project_observation``.  Every specialised query begins at the existing
    case-scoped Evidence -> Observation chain, so it cannot create an orphan.
    """
    if plan.status is not MappingStatus.APPLIED:
        return None

    claims = [
        {
            "claim_id": str(claim.claim_id),
            "role": claim.role,
            "properties": _mapping_claim_properties(observation, plan, claim),
        }
        for claim in plan.claims
    ]
    if claims:
        claims_query = (
            "MATCH (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
            "-[:YIELDED_OBSERVATION]->"
            "(o:Observation {case_id: $case_id, observation_id: $observation_id}) "
            "UNWIND $claims AS claim "
            "MERGE (s:SourceClaim {case_id: $case_id, claim_id: claim.claim_id}) "
            "SET s += claim.properties "
            "MERGE (o)-[r:PROJECTS_CLAIM "
            "{mapping_version: $mapping_version, role: claim.role}]->(s) "
            "SET r.observation_id = $observation_id, r.evidence_id = $evidence_id "
            "RETURN count(s) AS claim_count"
        )
        await repository.write(
            claims_query,
            {
                "case_id": str(observation.case_id),
                "evidence_id": str(observation.evidence_id),
                "observation_id": str(observation.observation_id),
                "mapping_version": plan.mapping_version,
                "claims": claims,
            },
        )

    if plan.event is None:
        return ProjectionResult(
            outcome=ProjectionOutcome.APPLIED,
            node_kind=GraphNodeKind.SOURCE_CLAIM,
            case_id=observation.case_id,
            domain_id=plan.claims[0].claim_id,
            relationships_upserted=len(claims),
        )

    event_properties: dict[str, Any] = {
        "projection_id": str(plan.event.projection_id),
        "case_id": str(observation.case_id),
        "evidence_id": str(observation.evidence_id),
        "observation_id": str(observation.observation_id),
        "source_observation_type": observation.observation_type,
        "event_type": plan.event.event_type,
        "extraction_confidence": observation.extraction_confidence,
        "extractor_name": observation.extractor.name,
        "extractor_version": observation.extractor.version,
        "extractor_config_hash": observation.extractor.config_hash,
        "extractor_model_version": observation.extractor.model_version,
        "mapping_version": plan.mapping_version,
        "mapping_config_hash": plan.mapping_config_hash,
        **_specialized_locator_properties(observation),
        **plan.event.properties,
    }
    if plan.event.event_time is not None:
        event_properties["event_time"] = plan.event.event_time
    if plan.event.time_window_start is not None:
        event_properties["time_window_start"] = plan.event.time_window_start
    if plan.event.time_window_end is not None:
        event_properties["time_window_end"] = plan.event.time_window_end
    if plan.media_lineage is not None:
        event_properties.update(
            {
                "media_chunk_id": str(plan.media_lineage.chunk_id),
                "media_manifest_id": str(plan.media_lineage.manifest_id),
                "media_manifest_hash": plan.media_lineage.manifest_hash,
                "media_processor_version": plan.media_lineage.processor_version,
                "media_configuration_hash": plan.media_lineage.configuration_hash,
                "media_artifact_ids": [str(value) for value in plan.media_lineage.artifact_ids],
            }
        )
    if claims:
        event_query = (
            "MATCH (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
            "-[:YIELDED_OBSERVATION]->"
            "(o:Observation {case_id: $case_id, observation_id: $observation_id}) "
            "UNWIND $claims AS claim "
            "OPTIONAL MATCH (s:SourceClaim {case_id: $case_id, claim_id: claim.claim_id}) "
            "WITH o, collect({node: s, role: claim.role}) AS participants, count(s) AS found, "
            "count(claim) AS requested "
            "WHERE found = requested "
            "MERGE (v:TemporalEvent {case_id: $case_id, projection_id: $projection_id}) "
            "SET v += $properties "
            "MERGE (o)-[source:PROJECTS_EVENT {mapping_version: $mapping_version}]->(v) "
            "SET source.observation_id = $observation_id, source.evidence_id = $evidence_id "
            "WITH v, participants "
            "UNWIND participants AS participant "
            "WITH v, participant.node AS source_claim, participant.role AS participant_role "
            "MERGE (v)-[r:HAS_CLAIM_PARTICIPANT {role: participant_role}]->(source_claim) "
            "SET r.mapping_version = $mapping_version "
            "RETURN v.projection_id AS projection_id"
        )
    else:
        event_query = (
            "MATCH (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
            "-[:YIELDED_OBSERVATION]->"
            "(o:Observation {case_id: $case_id, observation_id: $observation_id}) "
            "MERGE (v:TemporalEvent {case_id: $case_id, projection_id: $projection_id}) "
            "SET v += $properties "
            "MERGE (o)-[source:PROJECTS_EVENT {mapping_version: $mapping_version}]->(v) "
            "SET source.observation_id = $observation_id, source.evidence_id = $evidence_id "
            "RETURN v.projection_id AS projection_id"
        )
    await repository.write(
        event_query,
        {
            "case_id": str(observation.case_id),
            "evidence_id": str(observation.evidence_id),
            "observation_id": str(observation.observation_id),
            "projection_id": str(plan.event.projection_id),
            "mapping_version": plan.mapping_version,
            "claims": [{"claim_id": c["claim_id"], "role": c["role"]} for c in claims],
            "properties": event_properties,
        },
    )
    if plan.event.supporting_observation_ids or plan.event.contradictory_observation_ids:
        references = [
            {"observation_id": str(value), "kind": "supporting"}
            for value in plan.event.supporting_observation_ids
        ] + [
            {"observation_id": str(value), "kind": "contradictory"}
            for value in plan.event.contradictory_observation_ids
        ]
        reference_query = (
            "MATCH (v:TemporalEvent {case_id: $case_id, projection_id: $projection_id}) "
            "UNWIND $references AS reference "
            "MATCH (o:Observation {case_id: $case_id, observation_id: reference.observation_id}) "
            "MERGE (v)-[r:SUPPORTED_BY_OBSERVATION {kind: reference.kind}]->(o) "
            "SET r.mapping_version = $mapping_version "
            "RETURN count(o) AS reference_count"
        )
        await repository.write(
            reference_query,
            {
                "case_id": str(observation.case_id),
                "projection_id": str(plan.event.projection_id),
                "mapping_version": plan.mapping_version,
                "references": references,
            },
        )
    return ProjectionResult(
        outcome=ProjectionOutcome.APPLIED,
        node_kind=GraphNodeKind.TEMPORAL_EVENT,
        case_id=observation.case_id,
        domain_id=plan.event.projection_id,
        relationships_upserted=1 + len(claims),
    )
