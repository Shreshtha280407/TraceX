"""Unit tests for `app/modules/graph/projector.py`'s orchestration logic.

No real PostgreSQL or Neo4j involved -- `GraphProjectionOutboxRepository`
and `Neo4jGraphRepository` are both faked with minimal in-memory duck-typed
doubles, since `projector.py` only ever calls the methods each class
exposes. See `tests/integration/graph/` for tests against live
infrastructure, including the real `FOR UPDATE SKIP LOCKED` concurrency
behavior `claim_batch` depends on (not meaningfully fakeable in-process).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.contracts.observation import ExtractedEntityMention
from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.models import GraphProjectionJobRecord, GraphProjectionJobStatus
from app.modules.graph.projector import run_batch
from tests.fixtures.factories import make_evidence_record, make_observation

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _job(observation_id: UUID, *, case_id: UUID, evidence_id: UUID) -> GraphProjectionJobRecord:
    return GraphProjectionJobRecord(
        projection_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_id=observation_id,
        status=GraphProjectionJobStatus.RUNNING,
        attempt=1,
        max_attempts=5,
        lease_expires_at=_NOW,
        last_error_code=None,
        last_error_message=None,
        created_at=_NOW,
        updated_at=_NOW,
        completed_at=None,
    )


class _FakeOutbox:
    """Duck-typed stand-in for `GraphProjectionOutboxRepository`.

    `claim_batch` returns a fixed, pre-seeded list of jobs (the test itself
    decides what was "already claimed") -- this file tests `_project_one`'s
    per-job outcome logic via `run_batch`, not the claim query itself.
    """

    def __init__(self, jobs, observations, evidence) -> None:
        self._jobs = list(jobs)
        self.observations = observations
        self.evidence = evidence
        self.succeeded: list[UUID] = []
        self.retryable: list[tuple[UUID, str, GraphProjectionJobStatus]] = []
        self.failed: list[tuple[UUID, str]] = []

    async def claim_batch(self, *, now, lease_seconds, batch_size):
        return self._jobs[:batch_size]

    async def get_observation(self, observation_id):
        return self.observations.get(observation_id)

    async def get_evidence(self, case_id, evidence_id):
        return self.evidence.get((case_id, evidence_id))

    async def mark_succeeded(self, projection_id, now):
        self.succeeded.append(projection_id)

    async def mark_retryable_failure(
        self, projection_id, *, now, error_code, error_message, requeue_status
    ):
        self.retryable.append((projection_id, error_code, requeue_status))

    async def mark_failed(self, projection_id, *, now, error_code, error_message):
        self.failed.append((projection_id, error_code))


class _FakeGraph:
    """Duck-typed stand-in for `Neo4jGraphRepository`.

    `write_queue` is consumed in call order: each entry is either a list of
    result rows to return, or an `Exception` instance to raise. Read calls
    (used only by `_find_missing_entities`, never exercised by observation-
    only projection) always return an empty list.
    """

    def __init__(self, write_queue: list[Any]) -> None:
        self._write_queue = list(write_queue)
        self.write_calls: list[str] = []

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.write_calls.append(query)
        outcome = self._write_queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        return []


async def test_successful_projection_marks_job_succeeded() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    observation = make_observation(case_id=case_id, evidence_id=evidence_id)
    job = _job(observation.observation_id, case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(
        jobs=[job],
        observations={observation.observation_id: observation},
        evidence={(case_id, evidence_id): evidence},
    )
    graph = _FakeGraph(
        write_queue=[
            [{"evidence_id": str(evidence_id)}],  # project_evidence
            [{"observation_id": str(observation.observation_id)}],  # project_observation
            [{"mention_count": 1}],  # project_observation_mentions
        ]
    )

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.claimed == 1
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert outbox.succeeded == [job.projection_id]
    assert outbox.retryable == []
    assert outbox.failed == []


async def test_missing_observation_marks_job_failed_without_touching_graph() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    job = _job(uuid4(), case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(jobs=[job], observations={}, evidence={})
    graph = _FakeGraph(write_queue=[])

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.failed == 1
    assert outbox.failed == [(job.projection_id, "observation_not_found")]
    assert graph.write_calls == []  # never even attempted a graph write


async def test_missing_evidence_marks_job_failed_without_touching_graph() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    observation = make_observation(case_id=case_id, evidence_id=evidence_id)
    job = _job(observation.observation_id, case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(
        jobs=[job], observations={observation.observation_id: observation}, evidence={}
    )
    graph = _FakeGraph(write_queue=[])

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.failed == 1
    assert outbox.failed == [(job.projection_id, "evidence_not_found")]
    assert graph.write_calls == []


async def test_graph_connection_error_requeues_rather_than_fails() -> None:
    """A Neo4j outage leaves the job retryable -- never a durable `failed`, never lost data."""
    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    observation = make_observation(case_id=case_id, evidence_id=evidence_id)
    job = _job(observation.observation_id, case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(
        jobs=[job],
        observations={observation.observation_id: observation},
        evidence={(case_id, evidence_id): evidence},
    )
    graph = _FakeGraph(write_queue=[GraphConnectionError("graph write failed")])

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.retrying == 1
    assert summary.failed == 0
    assert outbox.retryable == [
        (job.projection_id, "graph_connection_error", GraphProjectionJobStatus.QUEUED)
    ]


async def test_observation_dependency_not_ready_marks_deferred() -> None:
    """`project_observation` DEFERRED (evidence dependency missing from the graph) requeues,
    distinctly from a connection error, as `deferred` rather than `queued`."""
    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    observation = make_observation(case_id=case_id, evidence_id=evidence_id)
    job = _job(observation.observation_id, case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(
        jobs=[job],
        observations={observation.observation_id: observation},
        evidence={(case_id, evidence_id): evidence},
    )
    graph = _FakeGraph(
        write_queue=[
            [{"evidence_id": str(evidence_id)}],  # project_evidence applies
            [],  # project_observation: no rows -> DEFERRED
        ]
    )

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.deferred == 1
    assert outbox.retryable == [
        (job.projection_id, "evidence_dependency_not_ready", GraphProjectionJobStatus.DEFERRED)
    ]


async def test_no_extracted_entities_still_succeeds_with_a_single_mentions_no_op() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    observation = make_observation(case_id=case_id, evidence_id=evidence_id, extracted_entities=[])
    job = _job(observation.observation_id, case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(
        jobs=[job],
        observations={observation.observation_id: observation},
        evidence={(case_id, evidence_id): evidence},
    )
    # Only 2 write calls expected: project_observation_mentions short-circuits
    # (no query at all) when there are no extracted_entities.
    graph = _FakeGraph(
        write_queue=[
            [{"evidence_id": str(evidence_id)}],
            [{"observation_id": str(observation.observation_id)}],
        ]
    )

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.succeeded == 1
    assert len(graph.write_calls) == 2


async def test_run_batch_processes_multiple_jobs_independently() -> None:
    """One job's outcome never affects another's -- a bad observation doesn't abort the batch."""
    case_id = uuid4()
    good_evidence_id, bad_evidence_id = uuid4(), uuid4()
    good_evidence = make_evidence_record(case_id=case_id, evidence_id=good_evidence_id)
    good_observation = make_observation(case_id=case_id, evidence_id=good_evidence_id)
    bad_observation = make_observation(case_id=case_id, evidence_id=bad_evidence_id)

    good_job = _job(good_observation.observation_id, case_id=case_id, evidence_id=good_evidence_id)
    bad_job = _job(bad_observation.observation_id, case_id=case_id, evidence_id=bad_evidence_id)

    outbox = _FakeOutbox(
        jobs=[good_job, bad_job],
        observations={
            good_observation.observation_id: good_observation,
            bad_observation.observation_id: bad_observation,
        },
        evidence={(case_id, good_evidence_id): good_evidence},  # bad_evidence_id absent
    )
    graph = _FakeGraph(
        write_queue=[
            [{"evidence_id": str(good_evidence_id)}],
            [{"observation_id": str(good_observation.observation_id)}],
            [{"mention_count": 1}],
        ]
    )

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.claimed == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert outbox.succeeded == [good_job.projection_id]
    assert outbox.failed == [(bad_job.projection_id, "evidence_not_found")]


async def test_run_batch_is_a_no_op_when_nothing_is_claimable() -> None:
    outbox = _FakeOutbox(jobs=[], observations={}, evidence={})
    graph = _FakeGraph(write_queue=[])

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.claimed == 0
    assert summary.succeeded == 0
    assert summary.failed == 0
    assert graph.write_calls == []


async def test_multi_mention_observation_projects_in_one_write_call() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    observation = make_observation(
        case_id=case_id,
        evidence_id=evidence_id,
        extracted_entities=[
            ExtractedEntityMention(text="Alice", entity_type_hint="person"),
            ExtractedEntityMention(text="Bob", entity_type_hint="person"),
        ],
    )
    job = _job(observation.observation_id, case_id=case_id, evidence_id=evidence_id)
    outbox = _FakeOutbox(
        jobs=[job],
        observations={observation.observation_id: observation},
        evidence={(case_id, evidence_id): evidence},
    )
    graph = _FakeGraph(
        write_queue=[
            [{"evidence_id": str(evidence_id)}],
            [{"observation_id": str(observation.observation_id)}],
            [{"mention_count": 2}],
        ]
    )

    summary = await run_batch(outbox, graph, now=_NOW, lease_seconds=120, batch_size=10)

    assert summary.succeeded == 1
    assert len(graph.write_calls) == 3  # evidence, observation, mentions -- one call each
