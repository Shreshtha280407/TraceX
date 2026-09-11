"""Evidence-lifecycle orchestration: upload -> hash -> store -> persist -> publish.

No FastAPI/HTTP concerns live here -- `api.py` maps the typed errors this
module raises onto HTTP responses, mirroring
`app.modules.access_control.service`'s split from `api.py`.

Ordering is deliberate and matches `docs/architecture/evidence-lifecycle.md`:
cheap request-shape validation (filename, routing, content-type) happens
before a single byte is streamed; the upload is hashed and size-checked
before anything is written to object storage; object storage is written
before anything is persisted to PostgreSQL; PostgreSQL persistence
(evidence + job, one transaction) happens before a job is ever published.
A failure at any step leaves no partially-visible state: storage writes are
cleaned up on a later failure, and nothing is published until persistence
has actually committed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy.exc
import structlog
from fastapi import UploadFile

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.worker import WorkerStatus
from app.modules.evidence_lifecycle.errors import (
    EmptyUploadError,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    JobNotFoundError,
    MissingFilenameError,
    PayloadTooLargeError,
    UnsupportedContentTypeError,
    UnsupportedSourceTypeError,
)
from app.modules.evidence_lifecycle.jobs import JobProducer
from app.modules.evidence_lifecycle.models import EvidenceRecord, WorkerJobRecord
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository
from app.modules.evidence_lifecycle.routing import accepted_content_types, route_for
from app.modules.evidence_lifecycle.storage import ObjectStorage, object_key_for

logger = structlog.get_logger(__name__)

#: Bounded read size for streaming upload -- bytes are never fully buffered
#: in memory at once regardless of the declared/actual upload size.
UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB
_MAX_FILENAME_LENGTH = 255


@dataclass(frozen=True)
class UploadContext:
    """Per-request, non-secret context threaded through to logs and audit events."""

    now: datetime
    request_id: str | None


@dataclass(frozen=True)
class UploadOutcome:
    evidence: EvidenceRecord
    job: WorkerJobRecord
    #: False when this call returned a cached result for a replayed `Idempotency-Key`.
    created: bool


class EvidenceLifecycleService:
    def __init__(
        self,
        repository: EvidenceLifecycleRepository,
        storage: ObjectStorage,
        job_producer: JobProducer,
        max_evidence_bytes: int,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._job_producer = job_producer
        self._max_evidence_bytes = max_evidence_bytes

    async def upload_evidence(
        self,
        *,
        case_id: UUID,
        uploaded_by: UUID,
        source_type: SourceType,
        classification: EvidenceClassification,
        parser_profile: str | None,
        upload: UploadFile,
        idempotency_key: str | None,
        context: UploadContext,
    ) -> UploadOutcome:
        filename = _safe_filename(upload.filename)
        route = route_for(source_type)
        if route is None:
            raise UnsupportedSourceTypeError(
                f"source_type '{source_type.value}' has no registered processor yet"
            )
        content_type = (upload.content_type or "").lower()
        if content_type not in accepted_content_types(source_type):
            raise UnsupportedContentTypeError(
                f"content_type '{content_type}' is not accepted for source_type "
                f"'{source_type.value}'"
            )

        sha256, size = await _hash_and_rewind(upload, self._max_evidence_bytes)
        if size == 0:
            raise EmptyUploadError("uploaded file is empty")

        if idempotency_key is not None:
            existing = await self._repository.get_evidence_by_idempotency_key(
                case_id, idempotency_key
            )
            if existing is not None:
                return await self._replay_or_conflict(existing, source_type, content_type, sha256)

        evidence_id = uuid4()
        object_key = object_key_for(case_id, evidence_id)

        logger.info(
            "evidence.upload.started",
            request_id=context.request_id,
            case_id=str(case_id),
            evidence_id=str(evidence_id),
            source_type=source_type.value,
        )

        await self._storage.put_object(object_key, upload.file, size, content_type)

        evidence = EvidenceRecord(
            evidence_id=evidence_id,
            case_id=case_id,
            source_type=source_type,
            original_filename=filename,
            content_type=content_type,
            object_uri=object_key,
            sha256=sha256,
            classification=classification,
            uploaded_by=uploaded_by,
            uploaded_at=context.now,
            parser_profile=parser_profile,
            # Evidence and its job are always created together in one
            # transaction (see `create_evidence_with_job`) -- there is no
            # separately-observable "uploaded but not yet queued" state.
            processing_status=EvidenceProcessingStatus.QUEUED,
            upload_idempotency_key=idempotency_key,
            created_at=context.now,
            updated_at=context.now,
        )
        job = _build_job(
            evidence_id=evidence_id,
            case_id=case_id,
            source_type=source_type,
            processor_name=route.processor_name,
            processor_version=route.processor_version,
            input_object_uri=object_key,
            now=context.now,
        )

        try:
            await self._repository.create_evidence_with_job(evidence, job)
        except sqlalchemy.exc.IntegrityError:
            await _safe_delete(self._storage, object_key, context.request_id)
            if idempotency_key is not None:
                existing = await self._repository.get_evidence_by_idempotency_key(
                    case_id, idempotency_key
                )
                if existing is not None:
                    return await self._replay_or_conflict(
                        existing, source_type, content_type, sha256
                    )
            raise
        except Exception:
            logger.error(
                "evidence.persistence.failed",
                request_id=context.request_id,
                case_id=str(case_id),
                evidence_id=str(evidence_id),
            )
            await _safe_delete(self._storage, object_key, context.request_id)
            raise

        logger.info(
            "evidence.job.created",
            request_id=context.request_id,
            case_id=str(case_id),
            evidence_id=str(evidence_id),
            job_id=str(job.job_id),
            processor_name=job.processor_name,
        )
        logger.info(
            "evidence.upload.succeeded",
            request_id=context.request_id,
            case_id=str(case_id),
            evidence_id=str(evidence_id),
        )

        dispatched_job = await self._dispatch(job, context)
        return UploadOutcome(evidence=evidence, job=dispatched_job, created=True)

    async def _replay_or_conflict(
        self,
        existing: EvidenceRecord,
        source_type: SourceType,
        content_type: str,
        sha256: str,
    ) -> UploadOutcome:
        if (
            existing.source_type is not source_type
            or existing.content_type != content_type
            or existing.sha256 != sha256
        ):
            raise IdempotencyConflictError(
                "Idempotency-Key was already used for a different upload"
            )
        job = await self._repository.get_job_by_evidence(existing.case_id, existing.evidence_id)
        if job is None:  # pragma: no cover - defensive: evidence+job are always created together
            raise EvidenceNotFoundError("evidence exists without a job record")
        return UploadOutcome(evidence=existing, job=job, created=False)

    async def _dispatch(self, job: WorkerJobRecord, context: UploadContext) -> WorkerJobRecord:
        """Publish `job`, returning the record reflecting the outcome.

        The caller's in-memory `job` predates this call and would otherwise
        report a stale `dispatched_at: null` even on a successful same-
        request publish -- returning the updated record (or the same one,
        on a deferred publish) keeps `UploadOutcome.job` accurate.
        """
        try:
            await self._job_producer.publish(job.to_contract())
        except Exception:
            logger.warning(
                "evidence.job.dispatch_deferred",
                request_id=context.request_id,
                case_id=str(job.case_id),
                job_id=str(job.job_id),
            )
            return job
        dispatched_at = datetime.now(UTC)
        await self._repository.mark_job_dispatched(job.job_id, dispatched_at)
        return job.model_copy(update={"dispatched_at": dispatched_at, "updated_at": dispatched_at})

    async def get_evidence(self, case_id: UUID, evidence_id: UUID) -> EvidenceRecord:
        record = await self._repository.get_evidence(case_id, evidence_id)
        if record is None:
            raise EvidenceNotFoundError("evidence not found")
        return record

    async def list_evidence(self, case_id: UUID) -> list[EvidenceRecord]:
        return await self._repository.list_evidence(case_id)

    async def get_job(self, case_id: UUID, job_id: UUID) -> WorkerJobRecord:
        record = await self._repository.get_job(case_id, job_id)
        if record is None:
            raise JobNotFoundError("job not found")
        return record


def _build_job(
    *,
    evidence_id: UUID,
    case_id: UUID,
    source_type: SourceType,
    processor_name: str,
    processor_version: str,
    input_object_uri: str,
    now: datetime,
) -> WorkerJobRecord:
    idempotency_key = f"{case_id}:{evidence_id}:{processor_name}:{processor_version}"
    return WorkerJobRecord(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=source_type,
        processor_name=processor_name,
        processor_version=processor_version,
        attempt=1,
        idempotency_key=idempotency_key,
        input_object_uri=input_object_uri,
        requested_at=now,
        status=WorkerStatus.QUEUED,
        queued_at=now,
        dispatched_at=None,
        last_error_code=None,
        last_error_message=None,
        last_error_retryable=None,
        created_at=now,
        updated_at=now,
    )


def _safe_filename(filename: str | None) -> str:
    if filename is None or not filename.strip():
        raise MissingFilenameError("filename is required")
    trimmed = filename.strip()
    if len(trimmed) > _MAX_FILENAME_LENGTH:
        raise MissingFilenameError("filename is too long")
    return trimmed


async def _hash_and_rewind(upload: UploadFile, max_bytes: int) -> tuple[str, int]:
    """Stream the upload in bounded chunks, hashing and size-checking as it goes.

    Never buffers the full upload in memory at once. Rewinds the
    underlying stream to the start afterward so `upload.file` can be
    handed directly to object storage without a second network round trip.
    """
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = await upload.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(f"upload exceeds the {max_bytes}-byte limit")
        hasher.update(chunk)
    await upload.seek(0)
    return hasher.hexdigest(), total


async def _safe_delete(storage: ObjectStorage, object_key: str, request_id: str | None) -> None:
    try:
        await storage.delete_object(object_key)
    except Exception:
        logger.error(
            "evidence.storage.cleanup_failed",
            request_id=request_id,
            object_key=object_key,
        )
