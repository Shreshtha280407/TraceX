"""Replay orchestration for typed Phase 5 graph-update events.

This module owns delivery mechanics only.  A registered handler owns the
semantic Neo4j projection and must use ``event.projection_key`` in its own
idempotent ``MERGE`` identities.  No correlation scoring or relationship
meaning is created here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.integration_models import (
    CorrelationProjectionContext,
    GraphUpdateEventRecord,
)
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository

ProjectionHandler = Callable[[CorrelationProjectionContext], Awaitable[None]]


@dataclass(frozen=True)
class GraphUpdateRunSummary:
    claimed: int
    succeeded: int = 0
    retrying: int = 0
    failed: int = 0


async def replay_graph_updates(
    repository: GraphCorrelationIntegrationRepository,
    handler: ProjectionHandler,
    *,
    lease_seconds: int = 120,
    batch_size: int = 25,
    now: datetime | None = None,
) -> GraphUpdateRunSummary:
    """Claim and replay a bounded batch without coupling PostgreSQL to Neo4j.

    Connection failures are deliberately returned to ``queued`` with no
    retry ceiling: valid PostgreSQL propositions remain replayable through a
    prolonged Neo4j outage.  Missing records are integrity failures and are
    terminal, with only a fixed safe error code persisted.
    """
    now = now or datetime.now(UTC)
    events = await repository.claim_events(
        now=now, lease_seconds=lease_seconds, batch_size=batch_size
    )
    succeeded = retrying = failed = 0
    for event in events:
        result = await _replay_one(repository, handler, event, now)
        if result == "succeeded":
            succeeded += 1
        elif result == "retrying":
            retrying += 1
        else:
            failed += 1
    return GraphUpdateRunSummary(
        claimed=len(events), succeeded=succeeded, retrying=retrying, failed=failed
    )


async def _replay_one(
    repository: GraphCorrelationIntegrationRepository,
    handler: ProjectionHandler,
    event: GraphUpdateEventRecord,
    now: datetime,
) -> str:
    context = await repository.projection_context(event)
    if context is None:
        await repository.mark_event_failed(event.event_id, now, "correlation_not_found")
        return "failed"
    try:
        await handler(context)
    except GraphConnectionError:
        await repository.mark_event_retryable(event.event_id, now, "graph_connection_error")
        return "retrying"
    except ValueError:
        # A handler may reject a malformed event payload, but raw details can
        # contain Cypher or a driver message and therefore never leave it.
        await repository.mark_event_failed(event.event_id, now, "projection_payload_invalid")
        return "failed"
    await repository.mark_event_succeeded(event.event_id, now)
    return "succeeded"
