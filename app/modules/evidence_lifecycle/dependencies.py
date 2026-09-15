"""FastAPI dependency providers for the evidence-lifecycle module.

Mirrors `app.modules.access_control.dependencies`'s pattern: the PostgreSQL
engine, MinIO-backed storage client, and Redis job producer are constructed
once at import time. All three underlying clients are lazy -- no real
connection opens until first used -- so this cannot fail import even if
none of postgres/minio/redis is reachable yet (readiness is `/readyz`'s
job, unaffected by this module).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import redis.asyncio as redis
from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event_safely
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.access_control.errors import WorkerSecurityConfigurationError
from app.modules.access_control.models import AuditOutcome, WorkerCredentialStatus
from app.modules.access_control.repository import AccessControlRepository
from app.modules.access_control.worker_credentials import (
    hash_worker_credential,
    resolve_worker_pepper,
)
from app.modules.evidence_lifecycle.jobs import JobProducer, RedisJobProducer
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository, create_engine
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService
from app.modules.evidence_lifecycle.storage import MinioObjectStorage, ObjectStorage
from app.modules.integrity.dependencies import get_integrity_service
from app.modules.integrity.service import IntegrityService

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
    integrity_recorder: Annotated[IntegrityService, Depends(get_integrity_service)],
) -> EvidenceLifecycleService:
    return EvidenceLifecycleService(
        repository=repository,
        storage=storage,
        job_producer=job_producer,
        max_evidence_bytes=settings.max_evidence_bytes,
        worker_lease_seconds=settings.worker_lease_seconds,
        worker_lease_max_seconds=settings.worker_lease_max_seconds,
        worker_job_max_attempts=settings.worker_job_max_attempts,
        graph_projection_max_attempts=settings.graph_projection_max_attempts,
        integrity_recorder=integrity_recorder,
    )


_WORKER_AUTH_DETAIL = "worker authentication required"
_WORKER_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class WorkerPrincipal:
    """A verified, per-worker service identity.

    Every field comes from a real `WorkerCredentialRecord`
    (`app.modules.access_control.models`) looked up by its authenticated
    credential digest -- never from anything the caller self-declared. See
    `docs/architecture/worker-identity-and-security.md`. Replaces the
    Phase 2.1 anonymous shared-secret stand-in of the same name.
    """

    worker_id: UUID
    display_name: str
    allowed_processor_names: tuple[str, ...]


async def require_worker_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    authorization: Annotated[str | None, Header()] = None,
) -> WorkerPrincipal:
    """Authenticate a caller against a real, revocable per-worker credential.

    Fails closed in every case, and every denial is audited as
    `worker_authentication_denied` (a failed audit write never changes the
    outcome -- see `record_audit_event_safely`):

    - The deployment requires a pepper (`WORKER_CREDENTIAL_PEPPER`) that
      isn't configured: `503` -- a deployment misconfiguration, not
      something a caller did wrong, checked *before* looking at the
      request at all.
    - Missing, blank, or malformed `Authorization` header: `401`.
    - A token whose digest matches no worker credential at all, or one
      that is `revoked`: `401`, in both cases the same generic detail --
      a caller can never learn from the response alone whether "this
      token never existed" or "this token existed but was revoked".

    Tests override this dependency directly via `app.dependency_overrides`
    for the happy path, and exercise the real dependency (with a fake
    `AccessControlRepository`) for the fail-closed paths -- the same
    pattern every other authenticated route in this codebase already uses.
    """
    now = datetime.now(UTC)
    request_id = get_request_id() or None

    try:
        pepper = resolve_worker_pepper(settings)
    except WorkerSecurityConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="worker security is not configured",
        ) from exc

    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not authorization or not authorization.startswith("Bearer ") or not token:
        await record_audit_event_safely(
            repository,
            event_type="worker_authentication_denied",
            outcome=AuditOutcome.DENIED,
            now=now,
            request_id=request_id,
            metadata={"reason": "missing_or_malformed_credential"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_WORKER_AUTH_DETAIL,
            headers=_WORKER_AUTH_HEADERS,
        )

    digest = hash_worker_credential(token, pepper)
    credential = await repository.get_worker_credential_by_digest(digest)
    if credential is None:
        await record_audit_event_safely(
            repository,
            event_type="worker_authentication_denied",
            outcome=AuditOutcome.DENIED,
            now=now,
            request_id=request_id,
            metadata={"reason": "invalid_credential"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_WORKER_AUTH_DETAIL,
            headers=_WORKER_AUTH_HEADERS,
        )
    if credential.status is not WorkerCredentialStatus.ACTIVE:
        await record_audit_event_safely(
            repository,
            event_type="worker_authentication_denied",
            outcome=AuditOutcome.DENIED,
            now=now,
            request_id=request_id,
            metadata={"reason": "revoked_credential", "worker_id": str(credential.worker_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_WORKER_AUTH_DETAIL,
            headers=_WORKER_AUTH_HEADERS,
        )

    return WorkerPrincipal(
        worker_id=credential.worker_id,
        display_name=credential.display_name,
        allowed_processor_names=credential.allowed_processor_names,
    )
