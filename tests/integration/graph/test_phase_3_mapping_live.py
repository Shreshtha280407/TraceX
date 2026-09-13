"""Live Neo4j checks for the Phase 3 deterministic mapping layer."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.common import Extractor, SourceLocator
from app.contracts.evidence import EvidenceRecordV1
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.graph.mapping import map_observation
from app.modules.graph.projection import (
    project_evidence,
    project_observation,
    project_specialized_mapping,
)
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.schema import apply_schema
from tests.fixtures.factories import make_evidence_record

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_EXTRACTOR = Extractor(
    name="phase3-live-fixture", version="1", config_hash="fixture", model_version="n/a"
)


def _observation(case_id: UUID, evidence: EvidenceRecordV1, kind: str) -> ObservationV1:
    attributes: dict[str, object]
    entities: list[ExtractedEntityMention] = []
    locator = SourceLocator(sheet="Synthetic", row=2)
    if kind == "document":
        attributes = {}
        entities = [ExtractedEntityMention(text="Synthetic Person", entity_type_hint="person")]
        locator = SourceLocator(page=1, span_start=8, span_end=24)
        observation_type = "ner_entity_mention"
    elif kind == "cdr":
        observation_type = "cdr_call_record"
        attributes = {
            "caller_number": "+919876543210",
            "callee_number": "+919123456789",
            "timestamp": "2026-01-01T04:30:00+00:00",
            "duration_seconds": 60.0,
        }
    else:
        observation_type = "financial_transaction_record"
        attributes = {
            "sender_account": "SYNTH-SOURCE-001",
            "receiver_account": "SYNTH-DEST-002",
            "amount": "100000.50",
            "currency": "INR",
            "timestamp": "2026-01-01T04:30:00+00:00",
        }
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence.evidence_id,
        observation_type=observation_type,
        extracted_entities=entities,
        attributes=attributes,  # type: ignore[arg-type]
        extraction_confidence=0.9,
        source_locator=locator,
        extractor=_EXTRACTOR,
        created_at=_NOW,
    )


async def test_phase_3_mapping_is_case_scoped_and_idempotent(
    repository: Neo4jGraphRepository, case_id: UUID
) -> None:
    await apply_schema(repository)
    evidence = make_evidence_record(case_id=case_id)
    observations = [
        _observation(case_id, evidence, kind) for kind in ("document", "cdr", "finance")
    ]

    await project_evidence(repository, evidence)
    for observation in observations:
        plan = map_observation(observation)
        await project_observation(repository, observation, plan)
        await project_specialized_mapping(repository, observation, plan)
        await project_specialized_mapping(repository, observation, plan)

    rows = await repository.read(
        "MATCH (e:Evidence {case_id: $case_id})-[:YIELDED_OBSERVATION]->"
        "(o:Observation {case_id: $case_id}) "
        "OPTIONAL MATCH (o)-[:PROJECTS_CLAIM]->(s:SourceClaim {case_id: $case_id}) "
        "OPTIONAL MATCH (o)-[:PROJECTS_EVENT]->(v:TemporalEvent {case_id: $case_id}) "
        "RETURN count(DISTINCT s) AS claims, count(DISTINCT v) AS events, "
        "count(DISTINCT o) AS observations",
        {"case_id": str(case_id)},
    )
    assert rows == [{"claims": 5, "events": 2, "observations": 3}]

    provenance = await repository.read(
        "MATCH (e:Evidence {case_id: $case_id})-[:YIELDED_OBSERVATION]->"
        "(o:Observation {case_id: $case_id}) "
        "-[:PROJECTS_EVENT]->(v:TemporalEvent {case_id: $case_id}) "
        "MATCH (v)-[:HAS_CLAIM_PARTICIPANT]->(s:SourceClaim {case_id: $case_id}) "
        "RETURN count(*) AS linked",
        {"case_id": str(case_id)},
    )
    assert provenance[0]["linked"] == 4
