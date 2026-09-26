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
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.observation_batch import ObservationBatchProgressV1
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    require_case_action,
    require_evidence_read,
)
from app.modules.access_control.models import (
    AuditOutcome,
    AuthorizedCasePrincipal,
    CaseAction,
    CaseRole,
    ClearanceLevel,
    clearance_satisfies,
)
from app.modules.access_control.repository import AccessControlRepository
from app.modules.evidence_lifecycle.dependencies import get_evidence_lifecycle_service
from app.modules.evidence_lifecycle.errors import (
    EmptyUploadError,
    EvidenceIntegrityUnavailableError,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    JobNotFoundError,
    MissingFilenameError,
    PayloadTooLargeError,
    UnsupportedContentTypeError,
    UnsupportedSourceTypeError,
)
from app.modules.evidence_lifecycle.models import (
    EvidenceRecord,
    WorkerJobRecord,
    WorkerResultRecord,
)
from app.modules.evidence_lifecycle.schemas import (
    EvidenceClassificationUpdateRequest,
    EvidenceIntegrityCheck,
    EvidenceLibraryItem,
    EvidenceLibraryResponse,
    EvidenceListResponse,
    EvidenceTypeOverrideRequest,
    EvidenceUploadResponse,
    EvidenceView,
    JobView,
)
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext

router = APIRouter(prefix="/api/v1/cases", tags=["evidence"])

#: Maps a per-evidence sensitivity label onto the case-membership clearance
#: ranks `policy.clearance_satisfies` already understands. `UNCLASSIFIED`
#: maps to `None` (no additional requirement beyond ordinary case-level
#: `EVIDENCE_READ`) -- `EvidenceClassification` has one more level than
#: `ClearanceLevel` and this frozen `app/contracts/evidence.py` enum is
#: never modified to add a matching one (G6's "per-evidence ABAC hook").
_CLASSIFICATION_TO_CLEARANCE: dict[EvidenceClassification, ClearanceLevel | None] = {
    EvidenceClassification.UNCLASSIFIED: None,
    EvidenceClassification.RESTRICTED: ClearanceLevel.RESTRICTED,
    EvidenceClassification.CONFIDENTIAL: ClearanceLevel.CONFIDENTIAL,
    EvidenceClassification.SECRET: ClearanceLevel.SECRET,
}

_CLASSIFICATION_RANK: dict[EvidenceClassification, int] = {
    EvidenceClassification.UNCLASSIFIED: -1,
    EvidenceClassification.RESTRICTED: 0,
    EvidenceClassification.CONFIDENTIAL: 1,
    EvidenceClassification.SECRET: 2,
}


def _evidence_visible_to(
    principal: AuthorizedCasePrincipal, classification: EvidenceClassification
) -> bool:
    required = _CLASSIFICATION_TO_CLEARANCE[classification]
    return required is None or clearance_satisfies(principal.membership.clearance, required)


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


def _job_view(
    job: WorkerJobRecord,
    *,
    result: WorkerResultRecord | None = None,
    observation_count: int = 0,
    latest_progress: ObservationBatchProgressV1 | None = None,
) -> JobView:
    progress_percent, current_stage = _job_progress_details(job, latest_progress)
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
        claimed_at=job.claimed_at,
        completed_at=result.completed_at if result is not None else None,
        observation_count=observation_count,
        last_error_code=job.last_error_code,
        last_error_message=job.last_error_message,
        latest_progress=latest_progress,
        progress_percent=progress_percent,
        current_stage=current_stage,
        started_at=job.claimed_at,
    )


def _job_progress_details(
    job: WorkerJobRecord, latest_progress: ObservationBatchProgressV1 | None
) -> tuple[int | None, str]:
    """Derive an honest display state exclusively from durable job data."""
    # ORM/contract records carry `WorkerStatus`; accepting its string form
    # too keeps this response adapter defensive around legacy rows/tests.
    status_value = getattr(job.status, "value", str(job.status))
    if status_value == "succeeded":
        return 100, "Ready"
    if status_value in {"failed", "deferred", "cancelled"}:
        return None, "Processing failed"
    if status_value == "queued":
        return None, "Waiting to start"
    if latest_progress is None:
        return None, "Processing evidence"
    percent = None
    if latest_progress.units_total and latest_progress.units_total > 0:
        percent = min(
            99, max(0, round(latest_progress.units_completed * 100 / latest_progress.units_total))
        )
    stage = latest_progress.stage
    if job.source_type is SourceType.DOCUMENT:
        label = "Extracting text" if stage in {"parsing", "normalizing"} else "Extracting entities"
    elif job.source_type is SourceType.IMAGE:
        label = "Running OCR"
    elif job.source_type is SourceType.VIDEO:
        label = "Analysing video"
    elif job.source_type is SourceType.AUDIO:
        label = "Transcribing audio"
    elif job.source_type in {
        SourceType.CHAT,
        SourceType.WHATSAPP_CHAT,
        SourceType.TELEGRAM_CHAT,
        SourceType.INSTAGRAM_CHAT,
    }:
        label = "Extracting entities"
    else:
        label = "Building evidence links" if stage == "submitting" else "Extracting text"
    return percent, label


@router.post(
    "/{case_id}/evidence",
    response_model=EvidenceUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_evidence(
    case_id: UUID,
    response: Response,
    file: UploadFile,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_write)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    source_type: Annotated[SourceType | None, Form()] = None,
    # Ordinary uploaders always inherit the case classification.  A Case
    # Owner may choose a higher one to protect a particular source, but can
    # never reduce it below the case baseline.
    classification: Annotated[EvidenceClassification | None, Form()] = None,
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

    case = await audit_repository.get_case(case_id)
    if case is None:  # defensive: case ABAC normally rejects this first
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    inherited_classification = EvidenceClassification(case.classification.value)
    evidence_classification = inherited_classification
    # Pre-library clients historically sent ``unclassified`` on every
    # multipart request. Treat that legacy placeholder as omitted: the
    # parent-case baseline still wins and no caller can lower classification.
    if classification is not None and classification is not EvidenceClassification.UNCLASSIFIED:
        if _CLASSIFICATION_RANK[classification] < _CLASSIFICATION_RANK[inherited_classification]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="evidence cannot be below case classification",
            )
        if (
            classification is not inherited_classification
            and principal.membership.role is not CaseRole.CASE_OWNER
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
        evidence_classification = classification
    context = UploadContext(now=datetime.now(UTC), request_id=get_request_id())
    try:
        outcome = await service.upload_evidence(
            case_id=case_id,
            uploaded_by=principal.principal.user_id,
            # Public upload never honours a client-selected type. Keep the
            # optional multipart field parseable only for old clients while
            # forcing byte inspection/routing in the service.
            source_type=None,
            classification=evidence_classification,
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
    """Per-evidence classification is enforced here too (G6): an item above the
    caller's clearance is silently omitted, never listed and then 403'd --
    a list must not leak the existence of evidence a caller cannot see.
    """
    records = await service.list_evidence(case_id)
    visible = [
        record for record in records if _evidence_visible_to(principal, record.classification)
    ]
    return EvidenceListResponse(items=tuple(_evidence_view(record) for record in visible))


@router.get("/{case_id}/evidence/library", response_model=EvidenceLibraryResponse)
async def evidence_library(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_read)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    access_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    query: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    source_type: SourceType | None = None,
    processing_status: EvidenceProcessingStatus | None = None,
    classification: EvidenceClassification | None = None,
    uploaded_by: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvidenceLibraryResponse:
    """Authorized case-scoped library/search view.

    Fetches an extra bounded page because evidence above the caller's
    clearance is removed before response construction.  It deliberately
    returns no global count, so inaccessible matches cannot be inferred.
    """
    records = await service.search_evidence(
        case_id,
        query=query,
        source_type=source_type.value if source_type else None,
        processing_status=processing_status.value if processing_status else None,
        classification=classification.value if classification else None,
        uploaded_by=uploaded_by,
        limit=limit + 1,
        offset=offset,
    )
    visible = [
        record for record in records if _evidence_visible_to(principal, record.classification)
    ]
    items: list[EvidenceLibraryItem] = []
    for record in visible[:limit]:
        job = await service.get_job_for_evidence(case_id, record.evidence_id)
        job_view = None
        if job is not None:
            result, observation_count = await service.get_job_result_summary(job.job_id)
            job_view = _job_view(
                job,
                result=result,
                observation_count=observation_count,
                latest_progress=await service.get_job_progress_summary(job.job_id),
            )
        uploader = await access_repository.get_user_by_id(record.uploaded_by)
        items.append(
            EvidenceLibraryItem(
                evidence=_evidence_view(record),
                job=job_view,
                preview=await service.get_evidence_preview(case_id, record.evidence_id),
                searchable=record.processing_status is EvidenceProcessingStatus.PROCESSED,
                uploader_display_name=uploader.display_name if uploader is not None else None,
            )
        )
    return EvidenceLibraryResponse(
        items=tuple(items), next_offset=offset + limit if len(records) > limit else None
    )


@router.get("/{case_id}/evidence/{evidence_id}/content")
async def stream_evidence_content(
    case_id: UUID,
    evidence_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_read)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    download: bool = False,
) -> StreamingResponse:
    """Authenticated source stream; never a MinIO or presigned public URL."""
    try:
        record = await service.get_evidence(case_id, evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="evidence not found"
        ) from exc
    if not _evidence_visible_to(principal, record.classification):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    try:
        _record, stream = await service.open_evidence_stream(case_id, evidence_id)
    except Exception as exc:  # storage errors are deliberately not exposed to callers
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="evidence source is unavailable"
        ) from exc
    disposition = "attachment" if download else "inline"
    safe_filename = quote(record.original_filename, safe="")
    return StreamingResponse(
        stream.chunks,
        media_type=record.content_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{safe_filename}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/{case_id}/evidence/{evidence_id}/classification", response_model=EvidenceView)
async def update_evidence_classification(
    case_id: UUID,
    evidence_id: UUID,
    body: EvidenceClassificationUpdateRequest,
    principal: Annotated[
        AuthorizedCasePrincipal, Depends(require_case_action(CaseAction.CASE_MANAGE))
    ],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    access_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> EvidenceView:
    if principal.membership.role is not CaseRole.CASE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    record = await service.get_evidence(case_id, evidence_id)
    if not _evidence_visible_to(principal, record.classification):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    case = await access_repository.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    if (
        _CLASSIFICATION_RANK[body.classification]
        < _CLASSIFICATION_RANK[EvidenceClassification(case.classification.value)]
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="evidence cannot be below case classification",
        )
    updated = await service.update_evidence_classification(
        case_id, evidence_id, classification=body.classification, now=datetime.now(UTC)
    )
    assert updated is not None
    await record_audit_event(
        access_repository,
        event_type="evidence.classification_update",
        outcome=AuditOutcome.SUCCESS,
        now=datetime.now(UTC),
        request_id=get_request_id(),
        user_id=principal.principal.user_id,
        case_id=case_id,
        metadata={"evidence_id": str(evidence_id), "classification": body.classification.value},
    )
    return _evidence_view(updated)


@router.patch(
    "/{case_id}/evidence/{evidence_id}/detected-type", response_model=EvidenceUploadResponse
)
async def correct_detected_type(
    case_id: UUID,
    evidence_id: UUID,
    body: EvidenceTypeOverrideRequest,
    principal: Annotated[
        AuthorizedCasePrincipal, Depends(require_case_action(CaseAction.CASE_MANAGE))
    ],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    access_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> EvidenceUploadResponse:
    if principal.membership.role is not CaseRole.CASE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    try:
        record = await service.get_evidence(case_id, evidence_id)
        if not _evidence_visible_to(principal, record.classification):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
        updated, job = await service.override_evidence_type(
            case_id, evidence_id, source_type=body.source_type, now=datetime.now(UTC)
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="evidence not found"
        ) from exc
    except (UnsupportedContentTypeError, UnsupportedSourceTypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    await record_audit_event(
        access_repository,
        event_type="evidence.detected_type_override",
        outcome=AuditOutcome.SUCCESS,
        now=datetime.now(UTC),
        request_id=get_request_id(),
        user_id=principal.principal.user_id,
        case_id=case_id,
        metadata={"evidence_id": str(evidence_id), "source_type": body.source_type.value},
    )
    return EvidenceUploadResponse(evidence=_evidence_view(updated), job=_job_view(job))


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
    if not _evidence_visible_to(principal, record.classification):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    return _evidence_view(record)


@router.get("/{case_id}/evidence/{evidence_id}/integrity", response_model=EvidenceIntegrityCheck)
async def get_evidence_integrity(
    case_id: UUID,
    evidence_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_read)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
) -> EvidenceIntegrityCheck:
    """Gap-Closure WP-4 (G7): re-hash the stored object and compare against
    the ingestion-time hash -- never trusts the stored value alone."""
    try:
        record = await service.get_evidence(case_id, evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="evidence not found"
        ) from exc
    if not _evidence_visible_to(principal, record.classification):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    try:
        return await service.verify_evidence_integrity(case_id, evidence_id)
    except EvidenceIntegrityUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post(
    "/{case_id}/evidence/{evidence_id}/reprocess",
    response_model=EvidenceUploadResponse,
)
async def reprocess_evidence(
    case_id: UUID,
    evidence_id: UUID,
    response: Response,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_evidence_write)],
    service: Annotated[EvidenceLifecycleService, Depends(get_evidence_lifecycle_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> EvidenceUploadResponse:
    """Gap-Closure WP-4 (G7): queue a new job for already-registered evidence.

    `Idempotency-Key` is required (unlike upload's optional one): a
    reprocess request has no file content to detect a replay from, so the
    caller's own key is the only signal distinguishing "retry my last
    reprocess request" from "queue a genuinely new attempt."
    """
    if len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key header is too long",
        )
    try:
        record = await service.get_evidence(case_id, evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="evidence not found"
        ) from exc
    if not _evidence_visible_to(principal, record.classification):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")

    try:
        job, is_new = await service.reprocess_evidence(
            case_id, evidence_id, idempotency_key=idempotency_key, now=datetime.now(UTC)
        )
    except UnsupportedSourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
    await record_audit_event(
        audit_repository,
        event_type="evidence.reprocess",
        outcome=AuditOutcome.SUCCESS,
        now=datetime.now(UTC),
        request_id=get_request_id(),
        user_id=principal.principal.user_id,
        case_id=case_id,
        metadata={"evidence_id": str(evidence_id), "job_id": str(job.job_id), "is_new": is_new},
    )
    return EvidenceUploadResponse(evidence=_evidence_view(record), job=_job_view(job))


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
    try:
        evidence = await service.get_evidence(case_id, record.evidence_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found") from exc
    if not _evidence_visible_to(principal, evidence.classification):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
    result, observation_count = await service.get_job_result_summary(job_id)
    latest_progress = await service.get_job_progress_summary(job_id)
    return _job_view(
        record,
        result=result,
        observation_count=observation_count,
        latest_progress=latest_progress,
    )
