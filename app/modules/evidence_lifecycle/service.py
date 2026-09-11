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
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy.exc
import structlog
from fastapi import UploadFile

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.worker import WorkerResultV1, WorkerStatus
from app.core.canonical import canonical_sha256
from app.modules.evidence_lifecycle.errors import (
    EmptyUploadError,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    InvalidClaimTokenError,
    JobNotFoundError,
    MissingFilenameError,
    PayloadTooLargeError,
    ResultConflictError,
    ResultValidationError,
    UnsupportedContentTypeError,
    UnsupportedSourceTypeError,
)
from app.modules.evidence_lifecycle.jobs import JobProducer
from app.modules.evidence_lifecycle.models import (
    TERMINAL_WORKER_STATUSES,
    EvidenceRecord,
    ObservationRecord,
    WorkerJobRecord,
    WorkerResultRecord,
)
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository
from app.modules.evidence_lifecycle.routing import accepted_content_types, route_for
from app.modules.evidence_lifecycle.storage import ObjectStorage, object_key_for

logger = structlog.get_logger(__name__)

#: Bounded read size for streaming upload -- bytes are never fully buffered
#: in memory at once regardless of the declared/actual upload size.
UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB
_MAX_FILENAME_LENGTH = 255
#: Bytes of entropy for a generated claim token (256 bits) -- mirrors
#: `access_control.tokens.REFRESH_TOKEN_BYTES`'s reasoning exactly.
CLAIM_TOKEN_BYTES = 32


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


@dataclass(frozen=True)
class ClaimOutcome:
    """A claimed job plus its one-time claim token, or nothing eligible right now.

    `claim_token` is transport metadata only -- never part of `WorkerJobV1`,
    never persisted raw (see `docs/architecture/worker-job-lifecycle.md`).
    """

    job: WorkerJobRecord | None
    claim_token: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class ClaimedEvidenceInput:
    """Safe evidence metadata for a currently-claimed job, plus its internal object key.

    `object_uri` is present here (unlike every *external* response shape in
    this module, e.g. `EvidenceView`) because this dataclass is consumed
    only by `internal_api.py`'s already-worker-authenticated,
    claim-token-verified input-stream route to open the byte stream -- it
    is never serialized directly into an HTTP response.
    """

    evidence_id: UUID
    content_type: str
    original_filename: str
    sha256: str
    source_type: SourceType
    parser_profile: str | None
    object_uri: str


@dataclass(frozen=True)
class ResultOutcome:
    job_id: UUID
    status: WorkerStatus
    result_id: UUID
    observation_ids: tuple[UUID, ...]
    #: False when this call returned the cached outcome of an identical, already-accepted replay.
    created: bool


class EvidenceLifecycleService:
    def __init__(
        self,
        repository: EvidenceLifecycleRepository,
        storage: ObjectStorage,
        job_producer: JobProducer,
        max_evidence_bytes: int,
        worker_lease_seconds: int = 300,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._job_producer = job_producer
        self._max_evidence_bytes = max_evidence_bytes
        self._worker_lease_seconds = worker_lease_seconds

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
            # Server-controlled, not the caller's `parser_profile` argument:
            # the persisted parser profile must always match the processor
            # this upload was actually routed to (`route`, decided above
            # from `source_type` alone) -- a client-supplied value here
            # would let a caller claim a profile inconsistent with its real
            # routing. The parameter itself is kept (existing call sites
            # pass `parser_profile=None` unchanged) but its value is never
            # stored -- see docs/architecture/phase-2-decisions.md.
            parser_profile=route.processor_name,
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

    async def get_job_result_summary(self, job_id: UUID) -> tuple[WorkerResultRecord | None, int]:
        """`(latest result row or None, observation count)` for the safe job-status view."""
        result = await self._repository.get_result_for_job(job_id)
        count = await self._repository.count_observations_for_job(job_id)
        return result, count

    # --- worker lifecycle: claim + result submission -----------------------

    async def claim_job(
        self, *, processor_name: str, processor_version: str, context: UploadContext
    ) -> ClaimOutcome:
        """Atomically claim one eligible job, generating a fresh one-time claim token.

        Returns an empty `ClaimOutcome` (never raises) when nothing is
        eligible right now -- "no work" is a normal outcome, not an error.
        """
        logger.info(
            "worker.job.claim_attempted",
            request_id=context.request_id,
            processor_name=processor_name,
            processor_version=processor_version,
        )
        claim_token = secrets.token_urlsafe(CLAIM_TOKEN_BYTES)
        claim_token_hash = _hash_claim_token(claim_token)
        claimed = await self._repository.claim_job(
            processor_name=processor_name,
            processor_version=processor_version,
            now=context.now,
            lease_seconds=self._worker_lease_seconds,
            claim_token_hash=claim_token_hash,
        )
        if claimed is None:
            logger.info(
                "worker.job.no_eligible_job",
                request_id=context.request_id,
                processor_name=processor_name,
                processor_version=processor_version,
            )
            return ClaimOutcome(job=None, claim_token=None, lease_expires_at=None)

        job, was_reclaim = claimed
        if was_reclaim:
            logger.warning(
                "worker.job.lease_expired_requeued",
                request_id=context.request_id,
                job_id=str(job.job_id),
                attempt=job.attempt,
            )
        logger.info(
            "worker.job.claimed",
            request_id=context.request_id,
            job_id=str(job.job_id),
            processor_name=processor_name,
            attempt=job.attempt,
        )
        return ClaimOutcome(job=job, claim_token=claim_token, lease_expires_at=job.lease_expires_at)

    async def submit_result(
        self,
        *,
        job_id: UUID,
        claim_token: str,
        result: WorkerResultV1,
        context: UploadContext,
    ) -> ResultOutcome:
        """Validate and durably persist one worker result, transitioning the job to terminal.

        Ordering matches `docs/architecture/worker-job-lifecycle.md`: the
        claim token is verified *first, unconditionally* -- including
        against an already-terminal job, so a wrong/unknown token can never
        retrieve a cached result it was never entitled to, and never
        distinguishes "wrong token" from "right token, different job" in
        its response. Only once the token genuinely matches does the flow
        branch: an already-terminal job goes to idempotent-replay/conflict
        comparison; a `running` job is checked for lease expiry and then
        the submitted result's own scope/status is validated -- all before
        any write is attempted.
        """
        logger.info(
            "worker.result.submit_attempted", request_id=context.request_id, job_id=str(job_id)
        )
        job = await self._repository.get_job_by_id(job_id)
        if job is None or job.claim_token_hash is None:
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="unknown_or_unclaimed_job",
            )
            raise InvalidClaimTokenError("invalid claim token")
        if not hmac.compare_digest(_hash_claim_token(claim_token), job.claim_token_hash):
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="token_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token")

        if job.status in TERMINAL_WORKER_STATUSES:
            return await self._replay_or_conflict_result(job, result, context)

        if job.status is not WorkerStatus.RUNNING:
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="not_claimed",
            )
            raise InvalidClaimTokenError("invalid claim token")
        if job.lease_expires_at is None or job.lease_expires_at < context.now:
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="lease_expired",
            )
            raise InvalidClaimTokenError("invalid claim token")

        _validate_result_scope(job, result)

        result_id = uuid4()
        payload = result.model_dump(mode="json")
        payload_hash = canonical_sha256(result)
        result_record = WorkerResultRecord(
            result_id=result_id,
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            attempt=job.attempt,
            status=result.status,
            derived_artifacts=[a.model_dump(mode="json") for a in result.derived_artifacts],
            checkpoint=result.checkpoint,
            error_code=result.error.code if result.error else None,
            error_message=result.error.message if result.error else None,
            error_retryable=result.error.retryable if result.error else None,
            canonical_payload=payload,
            payload_hash=payload_hash,
            completed_at=result.completed_at,
            created_at=context.now,
            updated_at=context.now,
        )
        observation_records = [
            ObservationRecord(
                observation_id=observation.observation_id,
                result_id=result_id,
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                observation_type=observation.observation_type,
                canonical_payload=observation.model_dump(mode="json"),
                created_at=context.now,
            )
            for observation in result.observations
        ]

        claim_token_hash = _hash_claim_token(claim_token)
        try:
            await self._repository.submit_result(
                job_id=job.job_id,
                expected_claim_token_hash=claim_token_hash,
                result=result_record,
                observations=observation_records,
            )
        except sqlalchemy.exc.IntegrityError:
            refreshed = await self._repository.get_job_by_id(job.job_id)
            if refreshed is not None and refreshed.status in TERMINAL_WORKER_STATUSES:
                return await self._replay_or_conflict_result(refreshed, result, context)
            logger.error(
                "worker.result.persistence_failed",
                request_id=context.request_id,
                job_id=str(job.job_id),
            )
            raise
        except Exception:
            logger.error(
                "worker.result.persistence_failed",
                request_id=context.request_id,
                job_id=str(job.job_id),
            )
            raise

        logger.info(
            "worker.result.accepted",
            request_id=context.request_id,
            job_id=str(job.job_id),
            status=result.status.value,
        )
        logger.info(
            "worker.job.marked_terminal",
            request_id=context.request_id,
            job_id=str(job.job_id),
            status=result.status.value,
        )
        return ResultOutcome(
            job_id=job.job_id,
            status=result.status,
            result_id=result_id,
            observation_ids=tuple(o.observation_id for o in result.observations),
            created=True,
        )

    async def _replay_or_conflict_result(
        self, job: WorkerJobRecord, result: WorkerResultV1, context: UploadContext
    ) -> ResultOutcome:
        existing = await self._repository.get_result_for_job(job.job_id)
        if existing is None:  # pragma: no cover - defensive: terminal implies a result exists
            raise InvalidClaimTokenError("invalid claim token")
        if existing.payload_hash != canonical_sha256(result):
            logger.warning(
                "worker.result.conflict", request_id=context.request_id, job_id=str(job.job_id)
            )
            raise ResultConflictError("a different result was already submitted for this job")
        logger.info(
            "worker.result.accepted",
            request_id=context.request_id,
            job_id=str(job.job_id),
            status=existing.status.value,
            idempotent_replay=True,
        )
        observations = await self._repository.list_observations_for_result(existing.result_id)
        return ResultOutcome(
            job_id=job.job_id,
            status=existing.status,
            result_id=existing.result_id,
            observation_ids=tuple(o.observation_id for o in observations),
            created=False,
        )

    # --- worker lifecycle: claimed-job input delivery -----------------------

    async def get_claimed_evidence_input(
        self, *, job_id: UUID, claim_token: str, context: UploadContext
    ) -> ClaimedEvidenceInput:
        """Validate a claim token against a currently-*running* job; return its evidence metadata.

        Deliberately narrower than `submit_result`'s claim-token check:
        input may only be streamed for a job that is *right now* `running`
        with an unexpired lease -- a never-claimed (`queued`), already-
        terminal, or lease-expired job is rejected with the same generic
        `InvalidClaimTokenError` every other claim-token failure mode uses
        (see `docs/architecture/worker-job-lifecycle.md`'s "Worker
        identity"/"Claim tokens" sections) -- the caller is never told
        *which* condition failed, or whether a different job exists.
        """
        logger.info(
            "worker.input.stream_attempted", request_id=context.request_id, job_id=str(job_id)
        )
        job = await self._repository.get_job_by_id(job_id)
        if job is None or job.claim_token_hash is None:
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="unknown_or_unclaimed_job",
            )
            raise InvalidClaimTokenError("invalid claim token")
        if not hmac.compare_digest(_hash_claim_token(claim_token), job.claim_token_hash):
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="token_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token")
        if job.status is not WorkerStatus.RUNNING:
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="not_claimed",
            )
            raise InvalidClaimTokenError("invalid claim token")
        if job.lease_expires_at is None or job.lease_expires_at < context.now:
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="lease_expired",
            )
            raise InvalidClaimTokenError("invalid claim token")

        evidence = await self._repository.get_evidence(job.case_id, job.evidence_id)
        if evidence is None:  # pragma: no cover - defensive: evidence+job always created together
            raise InvalidClaimTokenError("invalid claim token")

        logger.info(
            "worker.input.stream_authorized",
            request_id=context.request_id,
            job_id=str(job_id),
            evidence_id=str(evidence.evidence_id),
        )
        return ClaimedEvidenceInput(
            evidence_id=evidence.evidence_id,
            content_type=evidence.content_type,
            original_filename=evidence.original_filename,
            sha256=evidence.sha256,
            source_type=evidence.source_type,
            parser_profile=evidence.parser_profile,
            object_uri=evidence.object_uri,
        )


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
        claimed_at=None,
        lease_expires_at=None,
        claimed_by=None,
        claim_token_hash=None,
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


def _hash_claim_token(claim_token: str) -> str:
    """SHA-256 hex digest of a claim token, for storage/comparison.

    Same reasoning as `access_control.tokens.hash_refresh_token`: the input
    is already a 256-bit-entropy random secret, so a plain fast hash is
    the right tool, not a slow password KDF.
    """
    return hashlib.sha256(claim_token.encode("utf-8")).hexdigest()


def _validate_result_scope(job: WorkerJobRecord, result: WorkerResultV1) -> None:
    if (
        result.job_id != job.job_id
        or result.case_id != job.case_id
        or result.evidence_id != job.evidence_id
    ):
        raise ResultValidationError(
            "result job_id/case_id/evidence_id does not match the claimed job"
        )
    if result.status not in TERMINAL_WORKER_STATUSES:
        raise ResultValidationError("result status must be a terminal outcome")
    for observation in result.observations:
        if observation.case_id != job.case_id or observation.evidence_id != job.evidence_id:
            raise ResultValidationError(
                "an observation does not belong to the claimed job's case/evidence"
            )
