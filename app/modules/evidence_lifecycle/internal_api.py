"""Internal worker-lifecycle HTTP layer: `/api/v1/internal/worker-jobs/*`.

Distinct from `api.py`'s case-scoped, human-authenticated
`/api/v1/cases/{case_id}/...` routes in every way that matters: the caller
here is a worker (or worker test harness), not a case member; authorization
is `require_worker_principal` (a narrow, fail-closed shared-secret gate --
see `docs/architecture/worker-job-lifecycle.md`) plus, for result
submission and input delivery, a per-job one-time claim token -- never case
membership.

**Revised policy (Phase 2.2)**: TraceX never exposes raw evidence to users,
public clients, or generic internal callers, and never through an
object-storage URL or credential -- that boundary is unchanged. A worker
that has *actively claimed* a job may now receive that job's evidence bytes
through exactly one narrow, authenticated, claim-token-bound, job-specific
stream this module itself owns and serves (`GET /{job_id}/input`, below) --
the API remains the sole MinIO credential holder and streams the object
itself; a worker never receives an object key, bucket name, MinIO endpoint,
presigned URL, or storage credential. This is not a public download API: it
is valid only for the exact job a caller holds a live claim token for,
only while that job's lease is still active. See "Worker evidence
delivery" in `docs/architecture/evidence-lifecycle.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerResultV1, WorkerStatus
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event, record_audit_event_safely
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.access_control.models import AuditOutcome
from app.modules.access_control.repository import AccessControlRepository
from app.modules.evidence_lifecycle.dependencies import (
    WorkerPrincipal,
    get_evidence_lifecycle_service,
    get_object_storage,
    require_worker_principal,
)
from app.modules.evidence_lifecycle.errors import (
    InvalidClaimTokenError,
    ObservationBatchConflictError,
    ObservationBatchValidationError,
    ResultConflictError,
    ResultValidationError,
)
from app.modules.evidence_lifecycle.schemas import (
    ClaimRequest,
    ClaimResponse,
    RenewLeaseResponse,
    ResultAcknowledgement,
)
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import ObjectStorage

router = APIRouter(prefix="/api/v1/internal/worker-jobs", tags=["worker-internal"])

_CLAIM_TOKEN_HEADER = "X-Claim-Token"
_MAX_CLAIM_TOKEN_LENGTH = 200


@router.post("/claim", response_model=ClaimResponse)
async def claim_job(
    body: ClaimRequest,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> ClaimResponse:
    """Claim exactly one eligible job routed to the declared processor.

    `body.processor_name` must be one of `principal.allowed_processor_names`
    -- a worker credential scopes *which* processors a worker may claim, not
    just whether it may call this endpoint at all. A scope violation is a
    `403` (the caller is authenticated, just not authorized for this
    processor), audited as `worker_processor_scope_denied`, and never
    reaches the claim query at all -- no job is claimed on its behalf.

    Returns a safe "no work available" response (`job: null`) rather than
    an error when nothing is eligible. The `claim_token` returned here is
    shown to the caller exactly once -- only its hash is ever persisted.
    """
    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    if body.processor_name not in principal.allowed_processor_names:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_processor_scope_denied",
            outcome=AuditOutcome.DENIED,
            now=context.now,
            request_id=context.request_id,
            metadata={
                "worker_id": str(principal.worker_id),
                "processor_name": body.processor_name,
                "processor_version": body.processor_version,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this worker is not authorized for the requested processor",
        )

    outcome = await service.claim_job(
        processor_name=body.processor_name,
        processor_version=body.processor_version,
        context=context,
        claimed_by_worker_id=principal.worker_id,
    )

    # `record_audit_event_safely` (best-effort) throughout this block, not
    # the propagating `record_audit_event`: the state change itself (the
    # exhaustion sweep's terminal transition; this caller's own successful
    # claim) has already durably committed by this point. Unlike an
    # accepted result/batch, a claim's entire value to the caller is the
    # one-time `claim_token` in the response body -- letting an audit-sink
    # hiccup turn that into a 500 would strand an already-claimed job with
    # a token nobody received, recoverable only by waiting out its lease.
    for exhausted_job in outcome.retry_exhausted:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_job_retry_exhausted",
            outcome=AuditOutcome.SUCCESS,
            now=context.now,
            request_id=context.request_id,
            case_id=exhausted_job.case_id,
            metadata={
                "job_id": str(exhausted_job.job_id),
                "evidence_id": str(exhausted_job.evidence_id),
                "attempt": exhausted_job.attempt,
                "max_attempts": exhausted_job.max_attempts,
            },
        )

    if outcome.job is not None:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_job_reclaimed" if outcome.was_reclaim else "worker_job_claimed",
            outcome=AuditOutcome.SUCCESS,
            now=context.now,
            request_id=context.request_id,
            case_id=outcome.job.case_id,
            metadata={
                "worker_id": str(principal.worker_id),
                "job_id": str(outcome.job.job_id),
                "evidence_id": str(outcome.job.evidence_id),
                "processor_name": body.processor_name,
                "processor_version": body.processor_version,
                "attempt": outcome.job.attempt,
            },
        )

    return ClaimResponse(
        job=outcome.job.to_contract() if outcome.job is not None else None,
        claim_token=outcome.claim_token,
        lease_expires_at=outcome.lease_expires_at,
    )


@router.post("/{job_id}/result", response_model=ResultAcknowledgement)
async def submit_result(
    job_id: UUID,
    result: WorkerResultV1,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    claim_token: Annotated[str | None, Header(alias=_CLAIM_TOKEN_HEADER)] = None,
) -> ResultAcknowledgement:
    """Submit one terminal `WorkerResultV1` for a previously claimed job.

    Idempotent: an exact-payload resubmission for an already-completed job
    returns the original accepted outcome (never a duplicate write); a
    different payload for an already-completed job is a safe `409`. Only
    the worker identity currently bound to this job
    (`WorkerJobRecord.claimed_by_worker_id`, set at claim time) may submit
    its result -- a different, even fully-authenticated, worker presenting
    this job's valid claim token is rejected exactly like a wrong token
    (see `service.submit_result`'s ordering).
    """
    if claim_token is None or not claim_token.strip() or len(claim_token) > _MAX_CLAIM_TOKEN_LENGTH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid claim token")

    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        outcome = await service.submit_result(
            job_id=job_id,
            claim_token=claim_token,
            result=result,
            context=context,
            worker_id=principal.worker_id,
        )
    except InvalidClaimTokenError as exc:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_job_access_denied",
            outcome=AuditOutcome.DENIED,
            now=context.now,
            request_id=context.request_id,
            metadata={
                "worker_id": str(principal.worker_id),
                "job_id": str(job_id),
                "reason": exc.reason,
            },
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ResultValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ResultConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if outcome.created:
        await record_audit_event(
            audit_repository,
            event_type=(
                "worker_job_completed"
                if outcome.status is WorkerStatus.SUCCEEDED
                else "worker_job_failed"
            ),
            outcome=AuditOutcome.SUCCESS,
            now=context.now,
            request_id=context.request_id,
            user_id=None,
            case_id=result.case_id,
            metadata={
                "job_id": str(outcome.job_id),
                "evidence_id": str(result.evidence_id),
                "result_id": str(outcome.result_id),
                "status": outcome.status.value,
                "observation_count": len(outcome.observation_ids),
            },
        )

    return ResultAcknowledgement(
        job_id=outcome.job_id,
        status=outcome.status,
        result_id=outcome.result_id,
        observation_count=len(outcome.observation_ids),
        observation_ids=outcome.observation_ids,
    )


@router.post("/{job_id}/observations", response_model=ObservationBatchReceiptV1)
async def submit_observation_batch(
    job_id: UUID,
    submission: ObservationBatchSubmissionV1,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    claim_token: Annotated[str | None, Header(alias=_CLAIM_TOKEN_HEADER)] = None,
) -> ObservationBatchReceiptV1:
    """Submit one partial, provenance-rich observation micro-batch for a claimed job.

    Phase 3 (Nipun): the micro-batch counterpart to `/result` -- a worker
    processing a large document/CDR/finance source may call this any number
    of times while the job is still `running`, then submit exactly one
    terminal `WorkerResultV1` via `/result` as before (unchanged; this
    endpoint never transitions a job's status, `is_final_batch` is metadata
    only). Same authorization shape as `/result`/`/renew`: a real per-worker
    credential plus this exact job's claim token, and the caller must be the
    worker identity currently bound to the job.

    Idempotent: an exact-payload resubmission of an already-accepted
    `batch_id` returns the original accepted outcome (never a duplicate
    write, regardless of whether the job has since gone terminal); a
    different payload for the same `batch_id`, or the same
    `idempotency_key` reused for a different `batch_id`, is a safe `409`.
    See `docs/architecture/phase-3-decisions.md`.
    """
    if claim_token is None or not claim_token.strip() or len(claim_token) > _MAX_CLAIM_TOKEN_LENGTH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid claim token")

    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        outcome = await service.submit_observation_batch(
            job_id=job_id,
            claim_token=claim_token,
            submission=submission,
            context=context,
            worker_id=principal.worker_id,
        )
    except InvalidClaimTokenError as exc:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_job_access_denied",
            outcome=AuditOutcome.DENIED,
            now=context.now,
            request_id=context.request_id,
            metadata={
                "worker_id": str(principal.worker_id),
                "job_id": str(job_id),
                "reason": exc.reason,
            },
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except ObservationBatchValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ObservationBatchConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if outcome.status is BatchAcceptanceStatus.ACCEPTED:
        await record_audit_event(
            audit_repository,
            event_type="worker.observation_batch.accepted",
            outcome=AuditOutcome.SUCCESS,
            now=context.now,
            request_id=context.request_id,
            user_id=None,
            case_id=submission.case_id,
            metadata={
                "job_id": str(outcome.job_id),
                "batch_id": outcome.batch_id,
                "accepted_observation_count": outcome.accepted_observation_count,
            },
        )

    return ObservationBatchReceiptV1(
        job_id=outcome.job_id,
        batch_id=outcome.batch_id,
        status=outcome.status,
        accepted_observation_count=outcome.accepted_observation_count,
        progress=outcome.progress,
        request_id=context.request_id,
    )


@router.post("/{job_id}/renew", response_model=RenewLeaseResponse)
async def renew_job_lease(
    job_id: UUID,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    claim_token: Annotated[str | None, Header(alias=_CLAIM_TOKEN_HEADER)] = None,
) -> RenewLeaseResponse:
    """Extend a currently-claimed, still-`running` job's lease -- a heartbeat for a worker
    whose real processing may outlast the lease window it was claimed under.

    Same authorization shape as `/result`/`/input`: a real per-worker
    credential plus this exact job's claim token, and the caller must be
    the worker identity currently bound to the job. Renewing an unknown,
    wrong-token, wrong-worker, non-running, or already-lease-expired job is
    rejected uniformly via `InvalidClaimTokenError` -> generic `401`
    (audited as `worker_job_access_denied`, same as `/result`/`/input`'s
    denials) -- never distinguishing which condition failed, and never
    reviving a lease that has already expired (a legitimate reclaim by
    another worker always wins; see `service.renew_claim`).
    """
    if claim_token is None or not claim_token.strip() or len(claim_token) > _MAX_CLAIM_TOKEN_LENGTH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid claim token")

    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        renewal = await service.renew_claim(
            job_id=job_id,
            claim_token=claim_token,
            context=context,
            worker_id=principal.worker_id,
        )
    except InvalidClaimTokenError as exc:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_job_access_denied",
            outcome=AuditOutcome.DENIED,
            now=context.now,
            request_id=context.request_id,
            metadata={
                "worker_id": str(principal.worker_id),
                "job_id": str(job_id),
                "reason": exc.reason,
            },
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    await record_audit_event_safely(
        audit_repository,
        event_type="worker_job_lease_renewed",
        outcome=AuditOutcome.SUCCESS,
        now=context.now,
        request_id=context.request_id,
        case_id=renewal.case_id,
        metadata={
            "worker_id": str(principal.worker_id),
            "job_id": str(job_id),
            "evidence_id": str(renewal.evidence_id),
            "attempt": renewal.attempt,
        },
    )
    return RenewLeaseResponse(job_id=job_id, lease_expires_at=renewal.lease_expires_at)


@router.get("/{job_id}/input")
async def get_worker_job_input(
    job_id: UUID,
    principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    claim_token: Annotated[str | None, Header(alias=_CLAIM_TOKEN_HEADER)] = None,
) -> StreamingResponse:
    """Stream a currently-claimed job's evidence bytes to the worker that claimed it.

    Requires `require_worker_principal` (a real, active, per-worker
    credential), the exact claim token returned when *this* job was
    claimed, *and* that the caller is the worker identity currently bound
    to this job (`WorkerJobRecord.claimed_by_worker_id`) -- rejects a wrong
    job's token, a different worker's valid token, a never-claimed job, an
    already-terminal job, and an expired lease uniformly via
    `InvalidClaimTokenError` (never distinguishing which to the caller).
    Streamed in bounded chunks (`storage.open_stream`), never fully
    buffered in API memory. Response headers carry only the safe metadata
    a worker genuinely needs to parse and verify its input -- never an
    object key, bucket name, MinIO endpoint, presigned URL, or credential.
    """
    if claim_token is None or not claim_token.strip() or len(claim_token) > _MAX_CLAIM_TOKEN_LENGTH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid claim token")

    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        claimed_input = await service.get_claimed_evidence_input(
            job_id=job_id,
            claim_token=claim_token,
            context=context,
            worker_id=principal.worker_id,
        )
    except InvalidClaimTokenError as exc:
        await record_audit_event_safely(
            audit_repository,
            event_type="worker_job_access_denied",
            outcome=AuditOutcome.DENIED,
            now=context.now,
            request_id=context.request_id,
            metadata={
                "worker_id": str(principal.worker_id),
                "job_id": str(job_id),
                "reason": exc.reason,
            },
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    # A storage failure (including "object missing") propagates uncaught
    # to `app/core/errors.py`'s central handler -- the same safe-generic-500
    # path every other `StorageError` in this module already takes; never a
    # bespoke error here that might leak more than that handler already
    # guarantees not to.
    stream = await storage.open_stream(claimed_input.object_uri)

    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": _safe_content_disposition(claimed_input.original_filename),
        "X-TraceX-Evidence-Id": str(claimed_input.evidence_id),
        "X-TraceX-Evidence-SHA256": claimed_input.sha256,
        "X-TraceX-Source-Type": claimed_input.source_type.value,
    }
    if claimed_input.parser_profile:
        headers["X-TraceX-Parser-Profile"] = claimed_input.parser_profile
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)

    return StreamingResponse(stream.chunks, media_type=claimed_input.content_type, headers=headers)


def _safe_content_disposition(filename: str) -> str:
    """RFC 6266 `Content-Disposition`, safe against header injection and non-ASCII names.

    `original_filename` is caller-supplied display metadata (see
    `service._safe_filename`) -- trimmed and length-capped at upload time,
    but never sanitized against quotes or control characters, since it was
    never previously placed into a response header. Stripped/escaped here,
    at the one place that changes.
    """
    sanitized = filename.replace("\r", "").replace("\n", "").replace('"', "'")
    ascii_fallback = sanitized.encode("ascii", errors="replace").decode("ascii")
    encoded = quote(sanitized, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"
