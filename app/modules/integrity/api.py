"""Protected, case-scoped integrity verification HTTP surface.

This router deliberately exposes checkpoints, not a feed of source events.
Every database query binds the path's authorized ``case_id`` and all audit
metadata is identifiers/action/outcome only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event_safely
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    require_integrity_export,
    require_integrity_read,
    require_integrity_verify,
)
from app.modules.access_control.models import (
    AuditOutcome,
    AuthorizedCasePrincipal,
)
from app.modules.access_control.repository import AccessControlRepository
from app.modules.integrity.dependencies import get_integrity_service
from app.modules.integrity.models import (
    CheckpointSignatureRecord,
    MerkleCheckpointRecord,
    VerificationBundle,
    VerificationResult,
)
from app.modules.integrity.repository import IntegrityRepository
from app.modules.integrity.service import IntegrityService, IntegrityValidationError

router = APIRouter(prefix="/api/v1/cases", tags=["integrity"])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckpointSignatureView(_ResponseModel):
    """Public signature verification material; intentionally no private key exists here."""

    signature_id: UUID
    key_id: str
    algorithm: str
    signature_encoding: str
    public_key_b64: str
    public_key_fingerprint: str
    signed_root_hash: str
    signed_at: datetime


class IntegrityCheckpointView(_ResponseModel):
    checkpoint_id: UUID
    case_id: UUID
    start_sequence: int
    end_sequence: int
    leaf_count: int
    root_hash: str
    tree_format_version: str
    created_at: datetime
    signature: CheckpointSignatureView | None


class IntegrityCheckpointListResponse(_ResponseModel):
    case_id: UUID
    items: tuple[IntegrityCheckpointView, ...]
    limit: int = Field(ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(ge=0)


def _signature_view(signature: CheckpointSignatureRecord | None) -> CheckpointSignatureView | None:
    if signature is None:
        return None
    return CheckpointSignatureView(
        signature_id=signature.signature_id,
        key_id=signature.key_id,
        algorithm=signature.algorithm,
        signature_encoding=signature.signature_encoding,
        public_key_b64=signature.public_key_b64,
        public_key_fingerprint=signature.public_key_fingerprint,
        signed_root_hash=signature.signed_root_hash,
        signed_at=signature.signed_at,
    )


async def _checkpoint_view(
    repository: IntegrityRepository, checkpoint: MerkleCheckpointRecord
) -> IntegrityCheckpointView:
    return IntegrityCheckpointView(
        checkpoint_id=checkpoint.checkpoint_id,
        case_id=checkpoint.case_id,
        start_sequence=checkpoint.start_sequence,
        end_sequence=checkpoint.end_sequence,
        leaf_count=checkpoint.leaf_count,
        root_hash=checkpoint.root_hash,
        tree_format_version=checkpoint.tree_format_version,
        created_at=checkpoint.created_at,
        signature=_signature_view(await repository.get_signature(checkpoint.checkpoint_id)),
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="checkpoint not found")


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="integrity data temporarily unavailable",
    )


async def _audit_operation(
    repository: AccessControlRepository,
    principal: AuthorizedCasePrincipal,
    *,
    operation: str,
    checkpoint_id: UUID | None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
) -> None:
    metadata: dict[str, JsonValue] = {"action": operation}
    if checkpoint_id is not None:
        metadata["checkpoint_id"] = str(checkpoint_id)
    await record_audit_event_safely(
        repository,
        event_type="integrity_operation",
        outcome=outcome,
        now=datetime.now(UTC),
        request_id=get_request_id() or None,
        user_id=principal.principal.user_id,
        case_id=principal.case_id,
        metadata=metadata,
    )


@router.get("/{case_id}/integrity/checkpoints", response_model=IntegrityCheckpointListResponse)
async def list_checkpoints(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_integrity_read)],
    service: Annotated[IntegrityService, Depends(get_integrity_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IntegrityCheckpointListResponse:
    try:
        checkpoints = await service._repository.list_checkpoints(
            case_id, limit=limit, offset=offset
        )
        views = tuple([await _checkpoint_view(service._repository, item) for item in checkpoints])
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable() from exc
    await _audit_operation(
        audit_repository, principal, operation="integrity_read", checkpoint_id=None
    )
    return IntegrityCheckpointListResponse(case_id=case_id, items=views, limit=limit, offset=offset)


@router.get(
    "/{case_id}/integrity/checkpoints/{checkpoint_id}", response_model=IntegrityCheckpointView
)
async def get_checkpoint(
    case_id: UUID,
    checkpoint_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_integrity_read)],
    service: Annotated[IntegrityService, Depends(get_integrity_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> IntegrityCheckpointView:
    try:
        checkpoint = await service._repository.get_checkpoint(checkpoint_id, case_id=case_id)
        if checkpoint is None:
            raise _not_found()
        result = await _checkpoint_view(service._repository, checkpoint)
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable() from exc
    await _audit_operation(
        audit_repository, principal, operation="integrity_read", checkpoint_id=checkpoint_id
    )
    return result


@router.post(
    "/{case_id}/integrity/checkpoints/{checkpoint_id}/verify",
    response_model=VerificationResult,
)
async def verify_checkpoint(
    case_id: UUID,
    checkpoint_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_integrity_verify)],
    service: Annotated[IntegrityService, Depends(get_integrity_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> VerificationResult:
    try:
        # Do not return the service's "not found for this case" result as a
        # successful verification response: a normal 404 is the established
        # response for an authorized, but absent resource.
        if await service._repository.get_checkpoint(checkpoint_id, case_id=case_id) is None:
            raise _not_found()
        result = await service.verify_checkpoint(checkpoint_id, case_id=case_id)
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable() from exc
    await _audit_operation(
        audit_repository,
        principal,
        operation="integrity_verify",
        checkpoint_id=checkpoint_id,
        outcome=AuditOutcome.SUCCESS if result.ok else AuditOutcome.FAILURE,
    )
    return result


@router.get(
    "/{case_id}/integrity/checkpoints/{checkpoint_id}/export", response_model=VerificationBundle
)
async def export_checkpoint(
    case_id: UUID,
    checkpoint_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_integrity_export)],
    service: Annotated[IntegrityService, Depends(get_integrity_service)],
    audit_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> VerificationBundle:
    try:
        bundle = await service.export_verification_bundle(checkpoint_id, case_id=case_id)
    except IntegrityValidationError as exc:
        raise _not_found() from exc
    except sa.exc.SQLAlchemyError as exc:
        raise _unavailable() from exc
    await _audit_operation(
        audit_repository, principal, operation="integrity_export", checkpoint_id=checkpoint_id
    )
    return bundle
