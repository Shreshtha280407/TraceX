"""Orchestrates one durable projection attempt: outbox claim -> Neo4j write -> outcome.

`app.modules.graph.worker`'s `--once` CLI is the only caller of `run_batch`
in this repository today; `run_batch` itself has no daemon/loop of its own
-- see "No continuous projector daemon" in `docs/architecture/graph-projection.md`.

## Why evidence is projected on every attempt, not just the first

`project_evidence` (`projection.py`) is a plain `MERGE` with no dependency
of its own -- re-running it for an evidence node that's already correctly
projected is a safe no-op, not wasted or risky work. Projecting it
unconditionally on every attempt (rather than tracking "has this evidence
already been projected" separately) means `project_observation`'s own
dependency check can never spuriously defer just because a *different*
job for the same evidence hasn't run yet -- this job's own attempt is
always self-sufficient.

## Outcome -> durable status mapping

- `project_evidence`/`project_observation` succeed, mentions applied (or
  the observation had none) -> `mark_succeeded` (`SUCCEEDED`, terminal).
- `project_observation`/`project_observation_mentions` returns `DEFERRED`
  (the evidence dependency wasn't actually there despite this same attempt
  projecting it -- not expected in practice, but the safe path if it ever
  happens) -> `mark_retryable_failure(..., requeue_status=DEFERRED)`.
- A Neo4j read/write raises `GraphConnectionError` (Neo4j unreachable) ->
  `mark_retryable_failure(..., requeue_status=QUEUED)`.
- The canonical `ObservationV1`/`EvidenceRecordV1` row itself is missing
  from PostgreSQL (a data-integrity condition, not a transient one) ->
  `mark_failed` immediately, regardless of remaining attempts.

Every error message passed to the outbox is a short, fixed, human-written
string -- never a raw driver exception's text, Cypher query text, or a
stack trace (see `app.modules.graph.errors`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.mapping import map_observation
from app.modules.graph.models import (
    GraphProjectionJobRecord,
    GraphProjectionJobStatus,
    ProjectionOutcome,
)
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.projection import (
    project_evidence,
    project_observation,
    project_observation_mentions,
    project_specialized_mapping,
)
from app.modules.graph.repository import Neo4jGraphRepository


@dataclass(frozen=True)
class ProjectorAttemptResult:
    """The outcome of attempting exactly one claimed `GraphProjectionJobRecord`."""

    job: GraphProjectionJobRecord
    outcome: str  # "succeeded" | "failed" | "retrying" | "deferred"


@dataclass(frozen=True)
class ProjectorRunSummary:
    """Aggregate result of one `run_batch` call -- what `worker.py --once` reports."""

    claimed: int
    succeeded: int = 0
    failed: int = 0
    retrying: int = 0
    deferred: int = 0
    attempts: list[ProjectorAttemptResult] = field(default_factory=list)


async def _project_one(
    job: GraphProjectionJobRecord,
    outbox: GraphProjectionOutboxRepository,
    graph: Neo4jGraphRepository,
    now: datetime,
) -> str:
    observation = await outbox.get_observation(job.observation_id)
    if observation is None:
        await outbox.mark_failed(
            job.projection_id,
            now=now,
            error_code="observation_not_found",
            error_message="the canonical observation record was not found",
        )
        return "failed"

    evidence = await outbox.get_evidence(observation.case_id, observation.evidence_id)
    if evidence is None:
        await outbox.mark_failed(
            job.projection_id,
            now=now,
            error_code="evidence_not_found",
            error_message="the canonical evidence record was not found",
        )
        return "failed"

    try:
        await project_evidence(graph, evidence)
        mapping_plan = map_observation(observation)
        observation_result = await project_observation(graph, observation, mapping_plan)
        if observation_result.outcome is ProjectionOutcome.DEFERRED:
            await outbox.mark_retryable_failure(
                job.projection_id,
                now=now,
                error_code="evidence_dependency_not_ready",
                error_message="observation's evidence node was not present in the graph yet",
                requeue_status=GraphProjectionJobStatus.DEFERRED,
            )
            return "deferred"

        mentions_result = await project_observation_mentions(graph, observation)
        if mentions_result.outcome is ProjectionOutcome.DEFERRED:
            await outbox.mark_retryable_failure(
                job.projection_id,
                now=now,
                error_code="observation_dependency_not_ready",
                error_message="observation node was not present in the graph yet",
                requeue_status=GraphProjectionJobStatus.DEFERRED,
            )
            return "deferred"
        await project_specialized_mapping(graph, observation, mapping_plan)
    except GraphConnectionError:
        await outbox.mark_retryable_failure(
            job.projection_id,
            now=now,
            error_code="graph_connection_error",
            error_message="a Neo4j read or write failed",
            requeue_status=GraphProjectionJobStatus.QUEUED,
        )
        return "retrying"

    await outbox.mark_succeeded(job.projection_id, now)
    return "succeeded"


async def run_batch(
    outbox: GraphProjectionOutboxRepository,
    graph: Neo4jGraphRepository,
    *,
    now: datetime,
    lease_seconds: int,
    batch_size: int,
    renew_interval_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProjectorRunSummary:
    """Claim up to `batch_size` eligible jobs and attempt each one, once.

    Never raises on a single job's failure -- every attempt terminates in
    one of `mark_succeeded`/`mark_retryable_failure`/`mark_failed`, so one
    bad observation can never abort the rest of the batch.

    `renew_interval_seconds` (used by `graph.worker --loop`; `None` by
    default, unchanged behavior for the bounded `--once` CLI) is a
    heartbeat: every claimed job in one `claim_batch` call shares the same
    `lease_expires_at` set at claim time, so a batch whose *cumulative*
    processing time approaches that window risks a not-yet-reached job
    later in the batch looking lease-expired to another concurrent
    projector, which could then legitimately reclaim and duplicate it. When
    set, this renews every *not-yet-attempted* job's lease (extending it
    another `lease_seconds` from that moment) whenever more than
    `renew_interval_seconds` of wall-clock time has elapsed since the last
    renewal -- `monotonic` is injectable so tests can drive this
    deterministically without a real clock.
    """
    jobs = await outbox.claim_batch(now=now, lease_seconds=lease_seconds, batch_size=batch_size)
    counts = {"succeeded": 0, "failed": 0, "retrying": 0, "deferred": 0}
    attempts: list[ProjectorAttemptResult] = []
    last_renewal = monotonic()
    for index, job in enumerate(jobs):
        if (
            renew_interval_seconds is not None
            and monotonic() - last_renewal >= renew_interval_seconds
        ):
            renewal_now = datetime.now(UTC)
            for remaining_job in jobs[index:]:
                await outbox.renew_lease(
                    remaining_job.projection_id, now=renewal_now, lease_seconds=lease_seconds
                )
            last_renewal = monotonic()
        outcome = await _project_one(job, outbox, graph, now)
        attempts.append(ProjectorAttemptResult(job=job, outcome=outcome))
        counts[outcome] += 1
    return ProjectorRunSummary(
        claimed=len(jobs),
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        retrying=counts["retrying"],
        deferred=counts["deferred"],
        attempts=attempts,
    )
