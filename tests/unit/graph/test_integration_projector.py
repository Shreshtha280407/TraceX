"""Phase 5 outbox replay behavior without a live database or Neo4j."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import Extractor, SourceLocator
from app.modules.graph.errors import GraphConnectionError
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
from app.modules.graph.integration_projector import replay_graph_updates

_NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _context() -> CorrelationProjectionContext:
    case_id, evidence_id, observation_id, correlation_id, event_id = (uuid4() for _ in range(5))
    path = EvidencePathSnapshot(
        evidence_id=evidence_id,
        observation_id=observation_id,
        source_locator=SourceLocator(page=1),
        extractor=Extractor(
            name="fixture", version="1", config_hash="fixture", model_version="n/a"
        ),
        observation_created_at=_NOW,
    )
    correlation = CorrelationRecord(
        correlation_id=correlation_id,
        case_id=case_id,
        idempotency_key="fixture.correlation",
        correlation_type="reviewable_fixture",
        status=PropositionStatus.CANDIDATE,
        supporting_observation_ids=(observation_id,),
        evidence_paths=(path,),
        mapping_version="mapping.v1",
        config_version="config.v1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    event = GraphUpdateEventRecord(
        event_id=event_id,
        event_type=CORRELATION_EVENT_TYPE,
        schema_version=INTEGRATION_SCHEMA_VERSION,
        case_id=case_id,
        aggregate_type="correlation",
        aggregate_id=correlation_id,
        payload_reference=correlation_id,
        mapping_version="mapping.v1",
        config_version="config.v1",
        provenance_observation_ids=(observation_id,),
        projection_key="projection-key",
        idempotency_key="fixture.correlation",
        status=GraphUpdateEventStatus.RUNNING,
        attempt=1,
        lease_expires_at=_NOW,
        last_error_code=None,
        created_at=_NOW,
        updated_at=_NOW,
        completed_at=None,
    )
    return CorrelationProjectionContext(
        event=event, correlation=correlation, candidates=(), feature_snapshot=None
    )


class _FakeRepository:
    def __init__(self, context: CorrelationProjectionContext | None) -> None:
        self.context = context
        self.events = [context.event] if context else []
        self.succeeded = []
        self.retryable = []
        self.failed = []

    async def claim_events(self, *, now, lease_seconds, batch_size):
        return self.events[:batch_size]

    async def projection_context(self, event):
        return self.context

    async def mark_event_succeeded(self, event_id, now):
        self.succeeded.append(event_id)

    async def mark_event_retryable(self, event_id, now, error_code, *, deferred=False):
        self.retryable.append((event_id, error_code, deferred))

    async def mark_event_failed(self, event_id, now, error_code):
        self.failed.append((event_id, error_code))


async def test_replay_uses_stable_projection_identity_and_duplicate_delivery_is_safe() -> None:
    context = _context()
    repository = _FakeRepository(context)
    projected_keys: set[str] = set()

    async def handler(payload: CorrelationProjectionContext) -> None:
        # This is the exact idempotency contract a semantic Neo4j handler
        # receives: repeated delivery must MERGE by this stable key.
        projected_keys.add(payload.event.projection_key)

    first = await replay_graph_updates(repository, handler, now=_NOW)
    second = await replay_graph_updates(repository, handler, now=_NOW)

    assert first.succeeded == second.succeeded == 1
    assert projected_keys == {context.event.projection_key}
    assert repository.succeeded == [context.event.event_id, context.event.event_id]


async def test_neo4j_outage_keeps_durable_event_retryable_then_recovers() -> None:
    context = _context()
    repository = _FakeRepository(context)

    async def unavailable(_: CorrelationProjectionContext) -> None:
        raise GraphConnectionError("driver detail must not persist")

    failed_attempt = await replay_graph_updates(repository, unavailable, now=_NOW)
    assert failed_attempt.retrying == 1
    assert repository.retryable == [(context.event.event_id, "graph_connection_error", False)]
    assert repository.failed == []

    async def available(_: CorrelationProjectionContext) -> None:
        return None

    recovered = await replay_graph_updates(repository, available, now=_NOW)
    assert recovered.succeeded == 1
    assert repository.succeeded == [context.event.event_id]


async def test_missing_referenced_record_fails_safely_without_a_handler_call() -> None:
    repository = _FakeRepository(None)
    # A claimed row is supplied independently to model a resource deleted or
    # corrupted after enqueue but before replay.
    context = _context()
    repository.events = [context.event]
    calls = 0

    async def handler(_: CorrelationProjectionContext) -> None:
        nonlocal calls
        calls += 1

    summary = await replay_graph_updates(repository, handler, now=_NOW)
    assert summary.failed == 1
    assert calls == 0
    assert repository.failed == [(context.event.event_id, "correlation_not_found")]
