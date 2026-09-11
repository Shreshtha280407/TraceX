"""Internal worker-lifecycle HTTP layer: `/api/v1/internal/worker-jobs/*`.

Distinct from `api.py`'s case-scoped, human-authenticated
`/api/v1/cases/{case_id}/...` routes in every way that matters: the caller
here is a worker (or worker test harness), not a case member; authorization
is `require_worker_principal` (a narrow, fail-closed shared-secret gate --
see `docs/architecture/worker-job-lifecycle.md`) plus, for result
submission, a per-job one-time claim token -- never case membership. These
endpoints never expose a raw-evidence-download path; a worker gets exactly
`WorkerJobV1.input_object_uri` (already part of the frozen contract), never
a credential or a presigned URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.contracts.worker import WorkerResultV1
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.access_control.models import AuditOutcome
from app.modules.access_control.repository import AccessControlRepository
from app.modules.evidence_lifecycle.dependencies import (
    WorkerPrincipal,
    get_evidence_lifecycle_service,
    require_worker_principal,
)
from app.modules.evidence_lifecycle.errors import (
    InvalidClaimTokenError,
    ResultConflictError,
    ResultValidationError,
)
from app.modules.evidence_lifecycle.schemas import (
    ClaimRequest,
    ClaimResponse,
    ResultAcknowledgement,
)
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext

router = APIRouter(prefix="/api/v1/internal/worker-jobs", tags=["worker-internal"])

_CLAIM_TOKEN_HEADER = "X-Claim-Token"
_MAX_CLAIM_TOKEN_LENGTH = 200


@router.post("/claim", response_model=ClaimResponse)
async def claim_job(
    body: ClaimRequest,
    _principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
) -> ClaimResponse:
    """Claim exactly one eligible job routed to the declared processor.

    Returns a safe "no work available" response (`job: null`) rather than
    an error when nothing is eligible. The `claim_token` returned here is
    shown to the caller exactly once -- only its hash is ever persisted.
    """
    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    outcome = await service.claim_job(
        processor_name=body.processor_name,
        processor_version=body.processor_version,
        context=context,
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
    _principal: Annotated[WorkerPrincipal, Depends(require_worker_principal)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    claim_token: Annotated[str | None, Header(alias=_CLAIM_TOKEN_HEADER)] = None,
) -> ResultAcknowledgement:
    """Submit one terminal `WorkerResultV1` for a previously claimed job.

    Idempotent: an exact-payload resubmission for an already-completed job
    returns the original accepted outcome (never a duplicate write); a
    different payload for an already-completed job is a safe `409`.
    """
    if claim_token is None or not claim_token.strip() or len(claim_token) > _MAX_CLAIM_TOKEN_LENGTH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid claim token")

    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        outcome = await service.submit_result(
            job_id=job_id, claim_token=claim_token, result=result, context=context
        )
    except InvalidClaimTokenError as exc:
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
            event_type="worker.result.accepted",
            outcome=AuditOutcome.SUCCESS,
            now=context.now,
            request_id=context.request_id,
            user_id=None,
            case_id=result.case_id,
            metadata={
                "job_id": str(outcome.job_id),
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
