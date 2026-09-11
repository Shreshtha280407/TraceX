"""FastAPI dependency providers for the evidence-lifecycle module.

Mirrors `app.modules.access_control.dependencies`'s pattern: the PostgreSQL
engine, MinIO-backed storage client, and Redis job producer are constructed
once at import time. All three underlying clients are lazy -- no real
connection opens until first used -- so this cannot fail import even if
none of postgres/minio/redis is reachable yet (readiness is `/readyz`'s
job, unaffected by this module).
"""

from __future__ import annotations

from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends

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
    )
