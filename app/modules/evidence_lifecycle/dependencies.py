"""FastAPI dependency providers for the evidence-lifecycle module.

Mirrors `app.modules.access_control.dependencies`'s pattern: the PostgreSQL
engine, MinIO-backed storage client, and Redis job producer are constructed
once at import time. All three underlying clients are lazy -- no real
connection opens until first used -- so this cannot fail import even if
none of postgres/minio/redis is reachable yet (readiness is `/readyz`'s
job, unaffected by this module).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.modules.evidence_lifecycle.jobs import JobProducer, RedisJobProducer
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository, create_engine
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService
from app.modules.evidence_lifecycle.storage import MinioObjectStorage, ObjectStorage

_settings = get_settings()
_engine = create_engine(_settings)
_repository = EvidenceLifecycleRepository(_engine)
_storage: ObjectStorage = MinioObjectStorage(_settings)
_redis_client: redis.Redis = redis.from_url(str(_settings.redis_url))
_job_producer: JobProducer = RedisJobProducer(_redis_client)


def get_evidence_lifecycle_repository() -> EvidenceLifecycleRepository:
    return _repository


def get_object_storage() -> ObjectStorage:
    return _storage


def get_job_producer() -> JobProducer:
    return _job_producer


def get_evidence_lifecycle_service(
    repository: Annotated[EvidenceLifecycleRepository, Depends(get_evidence_lifecycle_repository)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    job_producer: Annotated[JobProducer, Depends(get_job_producer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvidenceLifecycleService:
    return EvidenceLifecycleService(
        repository=repository,
        storage=storage,
        job_producer=job_producer,
        max_evidence_bytes=settings.max_evidence_bytes,
        worker_lease_seconds=settings.worker_lease_seconds,
    )


@dataclass(frozen=True)
class WorkerPrincipal:
    """Marker proving a caller passed the internal worker-integration boundary.

    Deliberately carries no per-worker identity: there is no real
    per-worker credential-issuance system yet (that is Aditya-owned future
    work -- see `docs/architecture/worker-job-lifecycle.md`'s "Worker
    identity" section). This is a narrow, temporary shared-secret gate
    only, not a policy/RBAC system.
    """


async def require_worker_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> WorkerPrincipal:
    """Fail-closed shared-secret gate for `/api/v1/internal/worker-jobs/*`.

    Fails closed in two distinct ways, both deliberate: if
    `WORKER_SHARED_SECRET` is unset, every request is rejected (`503`) --
    there is no "unauthenticated is fine" fallback, ever; if it is set, a
    caller must present it exactly (`Authorization: Bearer <secret>`,
    constant-time compared) or is rejected (`401`). Tests override this
    dependency directly via `app.dependency_overrides`, the same pattern
    every other authenticated route in this codebase already uses.
    """
    configured_secret = settings.worker_shared_secret
    # `is None` is the normal case (Settings normalizes a blank value to
    # None); the emptiness check is defense in depth against that
    # normalization ever being bypassed -- an empty secret must never be
    # treated as "configured", or an empty `Authorization: Bearer ` header
    # would satisfy `hmac.compare_digest("", "")`.
    if configured_secret is None or not configured_secret.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="worker integration is not configured",
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, configured_secret.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="worker authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return WorkerPrincipal()
