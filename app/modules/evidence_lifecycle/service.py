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
from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchProgressV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerError, WorkerResultV1, WorkerStatus
from app.core.canonical import canonical_sha256
from app.modules.evidence_lifecycle.errors import (
    EmptyUploadError,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    InvalidClaimTokenError,
    JobNotFoundError,
    MediaManifestValidationError,
    MediaPublicationConflictError,
    MissingFilenameError,
    ObservationBatchConflictError,
    ObservationBatchValidationError,
    PayloadTooLargeError,
    ResultConflictError,
    ResultValidationError,
    UnsupportedContentTypeError,
    UnsupportedSourceTypeError,
)
from app.modules.evidence_lifecycle.jobs import JobProducer
from app.modules.evidence_lifecycle.media_orchestration import (
    ChunkManifest,
    MediaChunkPublication,
    chunk_identity,
)
from app.modules.evidence_lifecycle.models import (
    TERMINAL_WORKER_STATUSES,
    EvidenceRecord,
    MediaCheckpointRecord,
    ObservationBatchRecord,
    ObservationRecord,
    ObservationTransformationRecord,
    WorkerJobRecord,
    WorkerProgressEventRecord,
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
    `was_reclaim` distinguishes a job's first-ever claim from a lease-expiry
    reclaim -- `internal_api.py`'s route reads it to decide which audit
    event to record (`worker_job_claimed` vs `worker_job_reclaimed`).
    `retry_exhausted` lists every job this same call durably transitioned to
    `failed` because it had no attempts remaining -- a side effect of the
    opportunistic sweep every claim attempt runs first (see `claim_job`'s
    docstring); the route audits `worker_job_retry_exhausted` once per entry.
    """

    job: WorkerJobRecord | None
    claim_token: str | None
    lease_expires_at: datetime | None
    was_reclaim: bool = False
    retry_exhausted: tuple[WorkerJobRecord, ...] = ()


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
class RenewOutcome:
    """A successful lease renewal, plus the safe job context `internal_api.py` audits with."""

    lease_expires_at: datetime
    case_id: UUID
    evidence_id: UUID
    attempt: int


@dataclass(frozen=True)
class ResultOutcome:
    job_id: UUID
    status: WorkerStatus
    result_id: UUID
    observation_ids: tuple[UUID, ...]
    #: False when this call returned the cached outcome of an identical, already-accepted replay.
    created: bool


@dataclass(frozen=True)
class ObservationBatchOutcome:
    """The safe result of one `submit_observation_batch` call -- accepted or replayed."""

    job_id: UUID
    batch_id: str
    status: BatchAcceptanceStatus
    accepted_observation_count: int
    progress: ObservationBatchProgressV1 | None


@dataclass(frozen=True)
class MediaManifestOutcome:
    manifest_id: UUID
    created: bool


class EvidenceLifecycleService:
    def __init__(
        self,
        repository: EvidenceLifecycleRepository,
        storage: ObjectStorage,
        job_producer: JobProducer,
        max_evidence_bytes: int,
        worker_lease_seconds: int = 300,
        worker_lease_max_seconds: int = 3600,
        worker_job_max_attempts: int = 5,
        graph_projection_max_attempts: int = 5,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._job_producer = job_producer
        self._max_evidence_bytes = max_evidence_bytes
        self._worker_lease_seconds = worker_lease_seconds
        self._worker_lease_max_seconds = worker_lease_max_seconds
        self._worker_job_max_attempts = worker_job_max_attempts
        self._graph_projection_max_attempts = graph_projection_max_attempts

    async def create_media_manifest(
        self, *, manifest: ChunkManifest, context: UploadContext
    ) -> MediaManifestOutcome:
        """Persist an immutable coordinator manifest before workers publish chunks.

        This is intentionally a service seam, rather than a public API: the
        coordinator constructs no boundaries on a worker's behalf.  A retry
        with the identical canonical definition is harmless; changed content
        under its deterministic identity is rejected.
        """
        job = await self._repository.get_job_by_id(manifest.job_id)
        if (
            job is None
            or job.case_id != manifest.case_id
            or job.evidence_id != manifest.evidence_id
            or job.source_type.value != manifest.source_type
            or job.processor_name != manifest.processor_name
            or job.processor_version != manifest.processor_version
        ):
            raise MediaManifestValidationError("manifest does not belong to the declared job")
        created = await self._repository.create_media_manifest(manifest)
        if not created:
            existing = await self._repository.get_media_manifest(manifest.manifest_id)
            if existing is None or existing.manifest_hash != manifest.manifest_hash:
                raise MediaPublicationConflictError("a different manifest already exists")
        return MediaManifestOutcome(manifest_id=manifest.manifest_id, created=created)

    async def get_media_resume_checkpoint(
        self, *, case_id: UUID, job_id: UUID, manifest_id: UUID
    ) -> MediaCheckpointRecord | None:
        """Return the latest case-scoped durable checkpoint for a coordinator resume."""
        manifest = await self._repository.get_media_manifest(manifest_id)
        if manifest is None or manifest.case_id != case_id or manifest.job_id != job_id:
            raise MediaManifestValidationError("checkpoint manifest does not belong to this job")
        return await self._repository.get_latest_media_checkpoint(case_id, job_id, manifest_id)

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
            max_attempts=self._worker_job_max_attempts,
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
        self,
        *,
        processor_name: str,
        processor_version: str,
        context: UploadContext,
        claimed_by_worker_id: UUID | None = None,
    ) -> ClaimOutcome:
        """Atomically claim one eligible job, generating a fresh one-time claim token.

        `claimed_by_worker_id` (the authenticated worker's verified
        identity) is persisted unconditionally on a successful claim --
        including a reclaim, which is exactly how ownership legitimately
        transfers after a lease expires. Optional and defaults to `None`
        only so lower-level tests that don't care about worker identity
        don't need to thread one through; `internal_api.py`'s real HTTP
        endpoint always supplies a real value.

        Returns an empty `ClaimOutcome` (never raises) when nothing is
        eligible right now -- "no work" is a normal outcome, not an error.

        Before attempting the claim itself, opportunistically sweeps every
        job that is currently `running` with an expired lease and no
        attempts remaining (`repository.get_retry_exhausted_jobs`),
        transitioning each to `failed` via the existing `submit_result` path
        with a synthetic terminal `WorkerResultV1` (`error.code=
        "retry_exhausted"`) -- the same "reuse the established terminal-
        write path" reasoning `graph.outbox_repository.claim_batch`'s
        identical sweep already established for `graph_projection_jobs`.
        Every call to `claim_job`, regardless of which processor it's for,
        performs this sweep -- exhausted jobs for a *different* processor
        than the one being claimed right now are still swept, since nothing
        else in this repository ever calls it otherwise (mirrors
        `graph.outbox_repository`'s unscoped sweep too).
        """
        exhausted = await self._sweep_retry_exhausted_jobs(context)

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
            claimed_by_worker_id=claimed_by_worker_id,
        )
        if claimed is None:
            logger.info(
                "worker.job.no_eligible_job",
                request_id=context.request_id,
                processor_name=processor_name,
                processor_version=processor_version,
            )
            return ClaimOutcome(
                job=None, claim_token=None, lease_expires_at=None, retry_exhausted=exhausted
            )

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
        return ClaimOutcome(
            job=job,
            claim_token=claim_token,
            lease_expires_at=job.lease_expires_at,
            was_reclaim=was_reclaim,
            retry_exhausted=exhausted,
        )

    async def _sweep_retry_exhausted_jobs(
        self, context: UploadContext
    ) -> tuple[WorkerJobRecord, ...]:
        candidates = await self._repository.get_retry_exhausted_jobs(now=context.now)
        exhausted: list[WorkerJobRecord] = []
        for job in candidates:
            if job.claim_token_hash is None:  # pragma: no cover - defensive: running => claimed
                continue
            result = WorkerResultV1(
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                status=WorkerStatus.FAILED,
                observations=[],
                derived_artifacts=[],
                checkpoint=None,
                error=WorkerError(
                    code="retry_exhausted",
                    message=(
                        f"job exceeded its maximum of {job.max_attempts} claim/reclaim attempts"
                    ),
                    retryable=False,
                ),
                completed_at=context.now,
            )
            payload = result.model_dump(mode="json")
            result_record = WorkerResultRecord(
                result_id=uuid4(),
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                attempt=job.attempt,
                status=WorkerStatus.FAILED,
                derived_artifacts=[],
                checkpoint=None,
                error_code="retry_exhausted",
                error_message=result.error.message if result.error else None,
                error_retryable=False,
                canonical_payload=payload,
                payload_hash=canonical_sha256(result),
                completed_at=context.now,
                created_at=context.now,
                updated_at=context.now,
            )
            try:
                await self._repository.submit_result(
                    job_id=job.job_id,
                    expected_claim_token_hash=job.claim_token_hash,
                    result=result_record,
                    observations=[],
                    graph_projection_max_attempts=self._graph_projection_max_attempts,
                )
            except sqlalchemy.exc.IntegrityError:
                # A concurrent sweep (or the worker's own late /result call)
                # already made this job terminal -- not this call's problem.
                continue
            logger.warning(
                "worker.job.retry_exhausted",
                request_id=context.request_id,
                job_id=str(job.job_id),
                attempt=job.attempt,
                max_attempts=job.max_attempts,
            )
            exhausted.append(job)
        return tuple(exhausted)

    async def submit_result(
        self,
        *,
        job_id: UUID,
        claim_token: str,
        result: WorkerResultV1,
        context: UploadContext,
        worker_id: UUID | None = None,
    ) -> ResultOutcome:
        """Validate and durably persist one worker result, transitioning the job to terminal.

        Ordering matches `docs/architecture/worker-job-lifecycle.md`, now
        extended with a worker-identity check: the claim token is verified
        *first, unconditionally*, immediately followed by the worker-identity
        check (when `worker_id` is supplied) -- both *before* the
        already-terminal branch, so neither a wrong/unknown token nor the
        wrong worker's valid token can ever retrieve a cached result it
        wasn't entitled to. Only once both checks pass does the flow
        branch: an already-terminal job goes to idempotent-replay/conflict
        comparison; a `running` job is checked for lease expiry and then
        the submitted result's own scope/status is validated -- all before
        any write is attempted.

        `worker_id` defaults to `None` (meaning "skip the identity check")
        only so lower-level tests that don't care about worker identity
        don't need to thread one through; `internal_api.py`'s real HTTP
        endpoint always supplies the authenticated caller's real value.
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
            raise InvalidClaimTokenError("invalid claim token", reason="unknown_or_unclaimed_job")
        if not hmac.compare_digest(_hash_claim_token(claim_token), job.claim_token_hash):
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="token_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="token_mismatch")
        if worker_id is not None and job.claimed_by_worker_id != worker_id:
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="worker_identity_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="worker_identity_mismatch")

        if job.status in TERMINAL_WORKER_STATUSES:
            return await self._replay_or_conflict_result(job, result, context)

        if job.status is not WorkerStatus.RUNNING:
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="not_claimed",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="not_claimed")
        if job.lease_expires_at is None or job.lease_expires_at < context.now:
            logger.warning(
                "worker.result.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="lease_expired",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="lease_expired")

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
                observation_batch_id=None,
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
                graph_projection_max_attempts=self._graph_projection_max_attempts,
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
            raise InvalidClaimTokenError("invalid claim token", reason="missing_terminal_result")
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
        self,
        *,
        job_id: UUID,
        claim_token: str,
        context: UploadContext,
        worker_id: UUID | None = None,
    ) -> ClaimedEvidenceInput:
        """Validate a claim token against a currently-*running* job; return its evidence metadata.

        Deliberately narrower than `submit_result`'s claim-token check:
        input may only be streamed for a job that is *right now* `running`
        with an unexpired lease -- a never-claimed (`queued`), already-
        terminal, or lease-expired job is rejected with the same generic
        `InvalidClaimTokenError` every other claim-token failure mode uses
        (see `docs/architecture/worker-job-lifecycle.md`'s "Worker
        identity"/"Claim tokens" sections) -- the caller is never told
        *which* condition failed, or whether a different job exists. The
        worker-identity check (`worker_id`, when supplied) runs immediately
        after the claim-token check, exactly like `submit_result`.
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
            raise InvalidClaimTokenError("invalid claim token", reason="unknown_or_unclaimed_job")
        if not hmac.compare_digest(_hash_claim_token(claim_token), job.claim_token_hash):
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="token_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="token_mismatch")
        if worker_id is not None and job.claimed_by_worker_id != worker_id:
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="worker_identity_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="worker_identity_mismatch")
        if job.status is not WorkerStatus.RUNNING:
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="not_claimed",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="not_claimed")
        if job.lease_expires_at is None or job.lease_expires_at < context.now:
            logger.warning(
                "worker.input.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="lease_expired",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="lease_expired")

        evidence = await self._repository.get_evidence(job.case_id, job.evidence_id)
        if evidence is None:  # pragma: no cover - defensive: evidence+job always created together
            raise InvalidClaimTokenError("invalid claim token", reason="evidence_missing")

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

    async def renew_claim(
        self,
        *,
        job_id: UUID,
        claim_token: str,
        context: UploadContext,
        worker_id: UUID | None = None,
    ) -> RenewOutcome:
        """Extend a currently-claimed, still-`running` job's lease -- a heartbeat for a
        worker whose real processing (e.g. sampling and analyzing a long video, or a
        graph-projection batch) may outlast the lease window it was claimed under.

        Verification ordering matches `get_claimed_evidence_input`/`submit_result`
        exactly (unknown/unclaimed job -> token mismatch -> worker-identity
        mismatch -> not currently running -> lease already expired), all via
        the same generic `InvalidClaimTokenError` -- a caller attempting to
        renew a job it does not legitimately hold learns nothing about
        *why* renewal was refused. `worker_id` defaults to `None` (skip the
        identity check) for the same lower-level-test convenience every
        other method here already documents; `internal_api.py`'s real HTTP
        endpoint always supplies the authenticated caller's real value.
        """
        logger.info(
            "worker.lease.renew_attempted", request_id=context.request_id, job_id=str(job_id)
        )
        job = await self._repository.get_job_by_id(job_id)
        if job is None or job.claim_token_hash is None:
            logger.warning(
                "worker.lease.renew_rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="unknown_or_unclaimed_job",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="unknown_or_unclaimed_job")
        if not hmac.compare_digest(_hash_claim_token(claim_token), job.claim_token_hash):
            logger.warning(
                "worker.lease.renew_rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="token_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="token_mismatch")
        if worker_id is not None and job.claimed_by_worker_id != worker_id:
            logger.warning(
                "worker.lease.renew_rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="worker_identity_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="worker_identity_mismatch")
        if job.status is not WorkerStatus.RUNNING:
            logger.warning(
                "worker.lease.renew_rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="not_claimed",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="not_claimed")
        if job.lease_expires_at is None or job.lease_expires_at < context.now:
            logger.warning(
                "worker.lease.renew_rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="lease_expired",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="lease_expired")

        new_lease_expires_at = await self._repository.renew_lease(
            job_id,
            now=context.now,
            lease_seconds=self._worker_lease_seconds,
            max_lease_seconds=self._worker_lease_max_seconds,
        )
        if new_lease_expires_at is None:
            # Lost a race with a lease-expiry reclaim between the check above
            # and the atomic update itself -- vanishingly unlikely (the
            # window is microseconds), but the repository's own re-check is
            # unconditional, so this is still a real, honest possibility.
            logger.warning(
                "worker.lease.renew_rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="lease_expired",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="lease_expired")

        logger.info(
            "worker.lease.renewed",
            request_id=context.request_id,
            job_id=str(job_id),
            lease_expires_at=new_lease_expires_at.isoformat(),
        )
        return RenewOutcome(
            lease_expires_at=new_lease_expires_at,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            attempt=job.attempt,
        )

    # --- observation batches (Phase 3: partial micro-batch submission) ------

    async def submit_observation_batch(
        self,
        *,
        job_id: UUID,
        claim_token: str,
        submission: ObservationBatchSubmissionV1,
        context: UploadContext,
        worker_id: UUID | None = None,
        media_publication: MediaChunkPublication | None = None,
    ) -> ObservationBatchOutcome:
        """Validate and durably persist one partial observation micro-batch.

        Verification ordering: claim-token hash, then worker identity (both
        unconditional, exactly like `submit_result`/`renew_claim`), then
        `submission.job_id`/`case_id`/`evidence_id` against the claimed
        job's own values. Only *after* those pass does the flow branch on
        whether `(job_id, batch_id)` already names an accepted batch --
        exactly like `submit_result`'s "already terminal" branch, an
        existing batch's idempotent replay/conflict comparison runs
        regardless of the job's *current* status (a worker may legitimately
        retry a batch submission whose acknowledgement was lost, even after
        the job has since gone terminal via a separate `/result` call). Only
        a genuinely *new* batch_id requires the job to be `running` with an
        unexpired lease right now -- see
        `docs/architecture/phase-3-decisions.md`.

        `worker_id` defaults to `None` (skip the identity check) for the
        same lower-level-test convenience every other method here already
        documents; `internal_api.py`'s real HTTP endpoint always supplies
        the authenticated caller's real value.
        """
        logger.info(
            "worker.observation_batch.submit_attempted",
            request_id=context.request_id,
            job_id=str(job_id),
            batch_id=submission.batch_id,
        )
        job = await self._repository.get_job_by_id(job_id)
        if job is None or job.claim_token_hash is None:
            logger.warning(
                "worker.observation_batch.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="unknown_or_unclaimed_job",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="unknown_or_unclaimed_job")
        if not hmac.compare_digest(_hash_claim_token(claim_token), job.claim_token_hash):
            logger.warning(
                "worker.observation_batch.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="token_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="token_mismatch")
        if worker_id is not None and job.claimed_by_worker_id != worker_id:
            logger.warning(
                "worker.observation_batch.rejected",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="worker_identity_mismatch",
            )
            raise InvalidClaimTokenError("invalid claim token", reason="worker_identity_mismatch")

        if (
            submission.job_id != job.job_id
            or submission.case_id != job.case_id
            or submission.evidence_id != job.evidence_id
        ):
            raise ObservationBatchValidationError(
                "submission job_id/case_id/evidence_id does not match the claimed job"
            )

        if media_publication is not None:
            await self._validate_media_publication(job, submission, media_publication)

        existing = await self._repository.get_batch_by_job_and_batch_id(
            job.job_id, submission.batch_id
        )
        if existing is not None:
            outcome = await self._replay_or_conflict_batch(existing, submission, context)
            if media_publication is not None:
                await self._validate_media_replay(media_publication)
            return outcome

        existing_by_key = await self._repository.get_batch_by_job_and_idempotency_key(
            job.job_id, submission.idempotency_key
        )
        if existing_by_key is not None:
            # `existing_by_key.batch_id != submission.batch_id` is guaranteed here --
            # an equal batch_id would have matched the lookup above instead.
            logger.warning(
                "worker.observation_batch.conflict",
                request_id=context.request_id,
                job_id=str(job.job_id),
                reason="idempotency_key_reused_for_different_batch_id",
            )
            raise ObservationBatchConflictError(
                "idempotency_key was already used for a different batch_id"
            )

        if job.status is not WorkerStatus.RUNNING:
            logger.warning(
                "worker.observation_batch.rejected",
                request_id=context.request_id,
                job_id=str(job.job_id),
                reason="not_running",
            )
            raise ObservationBatchValidationError(
                "job is not currently accepting new observation batches"
            )
        if job.lease_expires_at is None or job.lease_expires_at < context.now:
            logger.warning(
                "worker.observation_batch.rejected",
                request_id=context.request_id,
                job_id=str(job.job_id),
                reason="lease_expired",
            )
            raise ObservationBatchValidationError("job's claim lease has expired")

        if submission.progress is not None:
            latest_progress = await self._repository.get_latest_progress_event(job.job_id)
            if (
                latest_progress is not None
                and latest_progress.attempt == job.attempt
                and (
                    submission.progress.units_completed < latest_progress.units_completed
                    or submission.progress.observations_emitted
                    < latest_progress.observations_emitted
                )
            ):
                raise ObservationBatchValidationError(
                    "progress must not regress within the same job attempt"
                )

        observation_batch_id = uuid4()
        batch_record = ObservationBatchRecord(
            observation_batch_id=observation_batch_id,
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            batch_id=submission.batch_id,
            batch_sequence=submission.batch_sequence,
            idempotency_key=submission.idempotency_key,
            is_final_batch=submission.is_final_batch,
            observation_count=len(submission.observations),
            payload_hash=canonical_sha256(submission),
            submitted_at=submission.submitted_at,
            created_at=context.now,
        )
        observation_records = [
            ObservationRecord(
                observation_id=observation.observation_id,
                result_id=None,
                observation_batch_id=observation_batch_id,
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                observation_type=observation.observation_type,
                canonical_payload=observation.model_dump(mode="json"),
                created_at=context.now,
            )
            for observation in submission.observations
        ]
        transformation_records = [
            ObservationTransformationRecord(
                transformation_id=transformation.transformation_id,
                observation_batch_id=observation_batch_id,
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                ordinal=transformation.ordinal,
                step_name=transformation.step_name,
                status=transformation.status,
                canonical_payload=transformation.model_dump(mode="json"),
                created_at=context.now,
            )
            for transformation in submission.transformations
        ]
        progress_event_record: WorkerProgressEventRecord | None = None
        if submission.progress is not None:
            progress_event_record = WorkerProgressEventRecord(
                progress_event_id=uuid4(),
                ordinal=0,  # placeholder -- server-assigned via nextval() on insert
                observation_batch_id=observation_batch_id,
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                attempt=job.attempt,
                stage=submission.progress.stage,
                units_total=submission.progress.units_total,
                units_completed=submission.progress.units_completed,
                observations_emitted=submission.progress.observations_emitted,
                batch_sequence=submission.progress.batch_sequence,
                message_code=submission.progress.message_code,
                occurred_at=submission.progress.occurred_at,
                created_at=context.now,
            )

        try:
            persisted_progress_event = await self._repository.submit_observation_batch(
                batch=batch_record,
                observations=observation_records,
                transformations=transformation_records,
                progress_event=progress_event_record,
                graph_projection_max_attempts=self._graph_projection_max_attempts,
                media_publication=media_publication,
            )
        except sqlalchemy.exc.IntegrityError:
            outcome = await self._resolve_batch_conflict(job.job_id, submission, context)
            if media_publication is not None:
                await self._validate_media_replay(media_publication)
            return outcome
        except Exception:
            logger.error(
                "worker.observation_batch.persistence_failed",
                request_id=context.request_id,
                job_id=str(job.job_id),
                batch_id=submission.batch_id,
            )
            raise

        progress_summary = _progress_contract(persisted_progress_event)
        if progress_summary is None:
            progress_summary = _progress_contract(
                await self._repository.get_latest_progress_event(job.job_id)
            )

        logger.info(
            "worker.observation_batch.accepted",
            request_id=context.request_id,
            job_id=str(job.job_id),
            batch_id=submission.batch_id,
            observation_count=len(observation_records),
        )
        return ObservationBatchOutcome(
            job_id=job.job_id,
            batch_id=submission.batch_id,
            status=BatchAcceptanceStatus.ACCEPTED,
            accepted_observation_count=len(observation_records),
            progress=progress_summary,
        )

    async def _validate_media_publication(
        self,
        job: WorkerJobRecord,
        submission: ObservationBatchSubmissionV1,
        publication: MediaChunkPublication,
    ) -> None:
        manifest = await self._repository.get_media_manifest(publication.manifest_id)
        if (
            manifest is None
            or manifest.case_id != job.case_id
            or manifest.evidence_id != job.evidence_id
            or manifest.job_id != job.job_id
            or manifest.manifest_hash != publication.manifest_hash
            or publication.batch != submission
        ):
            raise MediaManifestValidationError("media publication does not match its manifest/job")
        expected_chunk_id = chunk_identity(
            ChunkManifest.model_validate(manifest.canonical_payload), publication.chunk_index
        )
        if expected_chunk_id != publication.chunk_id:
            raise MediaManifestValidationError("media chunk identity does not match manifest order")
        if any(
            artifact.parent_evidence_id != job.evidence_id for artifact in publication.artifacts
        ):
            raise MediaManifestValidationError(
                "derived artifact parent is outside this evidence item"
            )
        chunk = await self._repository.get_media_chunk(publication.chunk_id)
        if (
            chunk is None
            or chunk.manifest_id != manifest.manifest_id
            or chunk.case_id != job.case_id
            or chunk.evidence_id != job.evidence_id
            or chunk.job_id != job.job_id
            or chunk.chunk_index != publication.chunk_index
        ):
            raise MediaManifestValidationError("media chunk does not belong to the manifest/job")
        if chunk.status == "completed" and chunk.publication_hash != canonical_sha256(publication):
            raise MediaPublicationConflictError(
                "a different payload was already accepted for this chunk"
            )

    async def _validate_media_replay(self, publication: MediaChunkPublication) -> None:
        chunk = await self._repository.get_media_chunk(publication.chunk_id)
        if chunk is None or chunk.publication_hash != canonical_sha256(publication):
            raise MediaPublicationConflictError(
                "media chunk replay does not match accepted content"
            )

    async def _replay_or_conflict_batch(
        self,
        existing: ObservationBatchRecord,
        submission: ObservationBatchSubmissionV1,
        context: UploadContext,
    ) -> ObservationBatchOutcome:
        if existing.payload_hash != canonical_sha256(submission):
            logger.warning(
                "worker.observation_batch.conflict",
                request_id=context.request_id,
                job_id=str(existing.job_id),
                batch_id=existing.batch_id,
            )
            raise ObservationBatchConflictError(
                "a different payload was already accepted for this batch_id"
            )
        logger.info(
            "worker.observation_batch.accepted",
            request_id=context.request_id,
            job_id=str(existing.job_id),
            batch_id=existing.batch_id,
            idempotent_replay=True,
        )
        latest_progress = await self._repository.get_latest_progress_event(existing.job_id)
        return ObservationBatchOutcome(
            job_id=existing.job_id,
            batch_id=existing.batch_id,
            status=BatchAcceptanceStatus.REPLAYED,
            accepted_observation_count=existing.observation_count,
            progress=_progress_contract(latest_progress),
        )

    async def _resolve_batch_conflict(
        self, job_id: UUID, submission: ObservationBatchSubmissionV1, context: UploadContext
    ) -> ObservationBatchOutcome:
        """Disambiguate an `IntegrityError` raised by `submit_observation_batch`.

        Mirrors `submit_result`'s re-read-then-decide pattern. Three root
        causes share this one exception, each resolved by re-querying:
        a `(job_id, batch_id)` race (replay-or-conflict, same as a
        pre-existing batch found up front), a `(job_id, idempotency_key)`
        collision under a different `batch_id` (always a conflict), or an
        `observation_id` already accepted under a completely different
        batch/result (always a conflict) -- see
        `docs/architecture/phase-3-decisions.md`.
        """
        existing = await self._repository.get_batch_by_job_and_batch_id(job_id, submission.batch_id)
        if existing is not None:
            return await self._replay_or_conflict_batch(existing, submission, context)

        existing_by_key = await self._repository.get_batch_by_job_and_idempotency_key(
            job_id, submission.idempotency_key
        )
        if existing_by_key is not None:
            logger.warning(
                "worker.observation_batch.conflict",
                request_id=context.request_id,
                job_id=str(job_id),
                reason="idempotency_key_reused_for_different_batch_id",
            )
            raise ObservationBatchConflictError(
                "idempotency_key was already used for a different batch_id"
            )

        logger.warning(
            "worker.observation_batch.conflict",
            request_id=context.request_id,
            job_id=str(job_id),
            reason="duplicate_observation_id",
        )
        raise ObservationBatchConflictError(
            "one or more observation_id values in this batch were already accepted under a "
            "different batch or result"
        )

    async def get_job_progress_summary(self, job_id: UUID) -> ObservationBatchProgressV1 | None:
        """The safe, current progress summary for a job -- its latest accepted event, if any."""
        return _progress_contract(await self._repository.get_latest_progress_event(job_id))


def _build_job(
    *,
    evidence_id: UUID,
    case_id: UUID,
    source_type: SourceType,
    processor_name: str,
    processor_version: str,
    input_object_uri: str,
    now: datetime,
    max_attempts: int,
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
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
        input_object_uri=input_object_uri,
        requested_at=now,
        status=WorkerStatus.QUEUED,
        queued_at=now,
        dispatched_at=None,
        claimed_at=None,
        lease_expires_at=None,
        claimed_by=None,
        claimed_by_worker_id=None,
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


def _progress_contract(
    record: WorkerProgressEventRecord | None,
) -> ObservationBatchProgressV1 | None:
    if record is None:
        return None
    return ObservationBatchProgressV1(
        stage=record.stage,
        units_total=record.units_total,
        units_completed=record.units_completed,
        observations_emitted=record.observations_emitted,
        batch_sequence=record.batch_sequence,
        message_code=record.message_code,
        occurred_at=record.occurred_at,
    )
