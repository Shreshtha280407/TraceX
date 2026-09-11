"""Durable job dispatch: publish a persisted `WorkerJobV1` onto Redis.

This is a producer only -- no consumer loop exists in this phase (see
`docs/architecture/evidence-lifecycle.md`). The `worker_jobs` PostgreSQL row
is the durable source of truth; a Redis publish on top of it is a
best-effort notification, so a temporarily-unavailable Redis never loses a
job -- `service.py` catches a publish failure, logs it, and leaves the
already-persisted row queued for a future redrive to pick up (a row with
`dispatched_at IS NULL` was never confirmed published).

Idempotency of publication is achieved one layer up: `service.py` only ever
calls `publish` once per durably-created job (a retried upload that matches
an existing `Idempotency-Key` returns the cached job without publishing
again), not via any dedup on the Redis side of this call itself.
"""

from __future__ import annotations

from typing import Protocol

import redis.asyncio as redis

from app.contracts.worker import WorkerJobV1
from app.core.canonical import canonical_bytes

#: One Redis LIST per source type, so a future consumer's `BLPOP` scopes
#: itself to the modality it actually knows how to process without needing
#: a broker-side routing layer.
_QUEUE_KEY_PREFIX = "tracex:jobs:"


def queue_key_for(source_type: str) -> str:
    return f"{_QUEUE_KEY_PREFIX}{source_type}"


class JobProducer(Protocol):
    async def publish(self, job: WorkerJobV1) -> None: ...


class RedisJobProducer:
    """Pushes a job's canonical JSON onto its source-type queue via `RPUSH`."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def publish(self, job: WorkerJobV1) -> None:
        await self._client.rpush(queue_key_for(job.source_type.value), canonical_bytes(job))


class FakeJobProducer:
    """In-memory `JobProducer` stand-in for unit tests.

    `fail` simulates a temporarily-unavailable Redis so `service.py`'s
    deferred-publication path is exercisable without a real broker.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[WorkerJobV1] = []
        self.fail = fail

    async def publish(self, job: WorkerJobV1) -> None:
        if self.fail:
            raise ConnectionError("simulated redis unavailability")
        self.published.append(job)
