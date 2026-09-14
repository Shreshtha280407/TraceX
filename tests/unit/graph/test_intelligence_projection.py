"""Focused semantic-handler tests with no live graph or source data."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import Extractor, SourceLocator
from app.modules.graph.integration_models import (
    CORRELATION_EVENT_TYPE,
    INTEGRATION_SCHEMA_VERSION,
    CorrelationProjectionContext,
    CorrelationRecord,
    EvidencePathSnapshot,
    GraphUpdateEventRecord,
    GraphUpdateEventStatus,
    PropositionStatus,
)
from app.modules.graph.intelligence.projection import (
    make_correlation_projection_handler,
    project_correlation_context,
)


class _Graph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def write(self, query: str, parameters: dict[str, object]) -> list[dict[str, object]]:
        self.calls.append((query, parameters))
        return [{"projection_key": parameters["projection_key"]}]


async def test_semantic_handler_uses_evidence_observation_chain_and_projection_key() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    case_id, evidence_id, observation_id, correlation_id, event_id = (uuid4() for _ in range(5))
    path = EvidencePathSnapshot(
        evidence_id=evidence_id,
        observation_id=observation_id,
        source_locator=SourceLocator(page=1),
        extractor=Extractor(
            name="synthetic", version="1", config_hash="fixture", model_version="n/a"
        ),
        observation_created_at=now,
    )
    context = CorrelationProjectionContext(
        correlation=CorrelationRecord(
            correlation_id=correlation_id,
            case_id=case_id,
            idempotency_key="synthetic.correlation",
            correlation_type="rules_candidate_correlation",
            status=PropositionStatus.NEEDS_REVIEW,
            supporting_observation_ids=(observation_id,),
            evidence_paths=(path,),
            mapping_version="phase5.v1",
            config_version="phase5.config.v1",
            created_at=now,
            updated_at=now,
        ),
        event=GraphUpdateEventRecord(
            event_id=event_id,
            event_type=CORRELATION_EVENT_TYPE,
            schema_version=INTEGRATION_SCHEMA_VERSION,
            case_id=case_id,
            aggregate_type="correlation",
            aggregate_id=correlation_id,
            payload_reference=correlation_id,
            mapping_version="phase5.v1",
            config_version="phase5.config.v1",
            provenance_observation_ids=(observation_id,),
            projection_key="synthetic-projection-key",
            idempotency_key="synthetic.correlation",
            status=GraphUpdateEventStatus.RUNNING,
            attempt=1,
            lease_expires_at=now,
            last_error_code=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        ),
        candidates=(),
        feature_snapshot=None,
    )
    graph = _Graph()
    await project_correlation_context(graph, context)  # type: ignore[arg-type]
    query, parameters = graph.calls[0]
    assert "YIELDED_OBSERVATION" in query
    assert "projection_key" in query
    assert parameters["evidence_paths"] == [
        {"evidence_id": str(evidence_id), "observation_id": str(observation_id)}
    ]
    assert "source_locator" not in str(parameters)


async def test_handler_factory_matches_nipun_replay_callback_shape() -> None:
    graph = _Graph()
    handler = make_correlation_projection_handler(graph)  # type: ignore[arg-type]
    assert callable(handler)
