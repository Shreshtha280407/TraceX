"""Evidence-lifecycle HTTP layer: the `/api/v1/cases/{case_id}` evidence/job routes.

Endpoints are thin: parse the request, call `EvidenceLifecycleService`,
translate typed errors from `errors.py` into safe, generic HTTP responses.
No business logic lives here -- mirrors `app.modules.access_control.api`'s
split from `service.py`.

Authorization reuses `app.modules.access_control.dependencies` exactly as
that module's own docstring intends ("the integration points Nipun's later
case/evidence endpoints ... depend on"): `require_case_action(EVIDENCE_WRITE)`
guards upload, `require_evidence_read` guards every read. A caller with no
active membership on `case_id`, or an insufficient role, gets the same
generic 403 every other case-scoped endpoint returns -- this router never
learns why a request was denied, by design (default-deny).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
    status,
)

from app.contracts.evidence import EvidenceClassification, SourceType
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    require_case_action,
    require_evidence_read,
)
from app.modules.access_control.models import AuditOutcome, AuthorizedCasePrincipal, CaseAction
from app.modules.access_control.repository import AccessControlRepository
from app.modules.evidence_lifecycle.dependencies import get_evidence_lifecycle_service
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
from app.modules.evidence_lifecycle.models import EvidenceRecord, WorkerJobRecord
from app.modules.evidence_lifecycle.schemas import (
    EvidenceListResponse,
    EvidenceUploadResponse,
    EvidenceView,
    JobView,
)
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext

router = APIRouter(prefix="/api/v1/cases", tags=["evidence"])

require_evidence_write = require_case_action(CaseAction.EVIDENCE_WRITE)

#: Bounds an obviously-malformed `Idempotency-Key` header before it ever
#: reaches a database query.
_MAX_IDEMPOTENCY_KEY_LENGTH = 200


def _evidence_view(evidence: EvidenceRecord) -> EvidenceView:
    return EvidenceView(
        evidence_id=evidence.evidence_id,
        case_id=evidence.case_id,
        source_type=evidence.source_type,
        original_filename=evidence.original_filename,
        content_type=evidence.content_type,
        sha256=evidence.sha256,
        classification=evidence.classification,
        uploaded_by=evidence.uploaded_by,
        uploaded_at=evidence.uploaded_at,
        parser_profile=evidence.parser_profile,
        processing_status=evidence.processing_status,
        created_at=evidence.created_at,
    )


def _job_view(job: WorkerJobRecord) -> JobView:
    return JobView(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        source_type=job.source_type,
        processor_name=job.processor_name,
        processor_version=job.processor_version,
        attempt=job.attempt,
        status=job.status,
        requested_at=job.requested_at,
        dispatched_at=job.dispatched_at,
        last_error_code=job.last_error_code,
        last_error_message=job.last_error_message,
    )


@router.post(
    "/{case_id}/evidence",
    response_model=EvidenceUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_evidence(
    case_id: UUID,
    response: Response,
    file: UploadFile,
    source_type: Annotated[SourceType, Form()],
    classification: Annotated[EvidenceClassification, Form()],
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_write)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    parser_profile: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EvidenceUploadResponse:
    """Upload one piece of case-scoped evidence.

    `Idempotency-Key` (optional): a retried request with the same key and
    the same file content returns the original result (`200`, no duplicate
    storage write or job) instead of `201`; the same key reused for
    genuinely different content is rejected with `409`.
    """
    if idempotency_key is not None and len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key header is too long",
        )

    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        outcome = await service.upload_evidence(
            case_id=case_id,
            uploaded_by=principal.principal.user_id,
            source_type=source_type,
            classification=classification,
            parser_profile=parser_profile,
            upload=file,
            idempotency_key=idempotency_key,
            context=context,
        )
    except (
        MissingFilenameError,
        UnsupportedSourceTypeError,
        UnsupportedContentTypeError,
        EmptyUploadError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except PayloadTooLargeError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await record_audit_event(
        audit_repository,
        event_type="evidence.upload",
        outcome=AuditOutcome.SUCCESS,
        now=context.now,
        request_id=context.request_id,
        user_id=principal.principal.user_id,
        case_id=case_id,
        metadata={
            "evidence_id": str(outcome.evidence.evidence_id),
            "job_id": str(outcome.job.job_id),
            "source_type": outcome.evidence.source_type.value,
            "sha256": outcome.evidence.sha256,
            "replay": not outcome.created,
        },
    )

    response.status_code = status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    return EvidenceUploadResponse(
        evidence=_evidence_view(outcome.evidence), job=_job_view(outcome.job)
    )


@router.get("/{case_id}/evidence", response_model=EvidenceListResponse)
async def list_evidence(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_read)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
) -> EvidenceListResponse:
    records = await service.list_evidence(case_id)
    return EvidenceListResponse(items=tuple(_evidence_view(record) for record in records))


@router.get("/{case_id}/evidence/{evidence_id}", response_model=EvidenceView)
async def get_evidence(
    case_id: UUID,
    evidence_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_read)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
) -> EvidenceView:
    try:
        record = await service.get_evidence(case_id, evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="evidence not found"
        ) from exc
    return _evidence_view(record)


@router.get("/{case_id}/jobs/{job_id}", response_model=JobView)
async def get_job(
    case_id: UUID,
    job_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_read)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
) -> JobView:
    try:
        record = await service.get_job(case_id, job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found") from exc
    return _job_view(record)
