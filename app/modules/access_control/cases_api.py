"""Case-management HTTP layer (G6): create a case, read it, add a member.

Shares the `/api/v1/cases` prefix with `evidence_lifecycle.api`, `graph.api`,
and `integrity.api` -- the same multi-router-same-prefix pattern already in
use across those modules; none of this router's paths collide with theirs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import Settings, get_settings
from app.core.errors import get_request_id
from app.core.pagination import (
    CursorError,
    CursorPosition,
    decode_cursor,
    derive_cursor_signing_key,
    encode_cursor,
)
from app.modules.access_control import case_service, notes_service
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_case_note_repository,
    require_authenticated_user,
    require_case_note_write,
    require_case_read,
    require_member_manage,
)
from app.modules.access_control.errors import ValidationError
from app.modules.access_control.models import (
    AuthenticatedPrincipal,
    AuthorizedCasePrincipal,
    CaseAuditEventListResponse,
    CaseCreateRequest,
    CaseMemberAddRequest,
    CaseMemberCandidateListResponse,
    CaseMemberCandidateView,
    CaseMemberListResponse,
    CaseMemberUpdateRequest,
    CaseMemberView,
    CaseStatusView,
    CaseView,
)
from app.modules.access_control.notes_models import (
    CaseNoteCreateRequest,
    CaseNoteListResponse,
    CaseNoteRecord,
)
from app.modules.access_control.notes_repository import CaseNoteRepository
from app.modules.access_control.repository import AccessControlRepository
from app.modules.integrity.dependencies import get_integrity_service
from app.modules.integrity.service import IntegrityService

router = APIRouter(prefix="/api/v1/cases", tags=["cases"])


@router.post("", response_model=CaseView, status_code=status.HTTP_201_CREATED)
async def create_case(
    body: CaseCreateRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseView:
    """Any authenticated, active user may create a case -- they become its `CASE_OWNER`.

    Not case-scoped (the case doesn't exist yet), so this does not go
    through `require_case_action` -- only `require_authenticated_user`.
    """
    try:
        return await case_service.create_case(
            repository,
            body,
            creator_user_id=principal.user_id,
            now=datetime.now(UTC),
            request_id=get_request_id(),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{case_id}", response_model=CaseView)
async def get_case(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_case_read)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseView:
    view = await case_service.get_case_view(repository, case_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return view


@router.get("/{case_id}/status", response_model=CaseStatusView)
async def get_case_status(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_case_read)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseStatusView:
    view = await case_service.get_case_status(repository, case_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return view


@router.post(
    "/{case_id}/members", response_model=CaseMemberView, status_code=status.HTTP_201_CREATED
)
async def add_case_member(
    case_id: UUID,
    body: CaseMemberAddRequest,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_member_manage)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseMemberView:
    """Only `CASE_OWNER`/`CASE_MANAGER` reach here -- `require_member_manage` enforces it."""
    try:
        return await case_service.add_case_member(
            repository,
            case_id,
            body,
            added_by_user_id=principal.principal.user_id,
            now=datetime.now(UTC),
            request_id=get_request_id(),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{case_id}/members", response_model=CaseMemberListResponse)
async def list_case_members(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_member_manage)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseMemberListResponse:
    """Directory is restricted to owner/manager; it is never a global account listing."""
    return CaseMemberListResponse(
        items=tuple(await case_service.list_case_members(repository, case_id))
    )


@router.get("/{case_id}/member-candidates", response_model=CaseMemberCandidateListResponse)
async def list_case_member_candidates(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_member_manage)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> CaseMemberCandidateListResponse:
    """The account picker exposes only active account name/email/ID after case authorization."""
    users = await repository.list_active_user_candidates(limit=limit)
    return CaseMemberCandidateListResponse(
        items=tuple(
            CaseMemberCandidateView(
                user_id=user.user_id,
                display_name=user.display_name,
                email_normalized=user.email_normalized,
            )
            for user in users
        )
    )


@router.patch("/{case_id}/members/{user_id}", response_model=CaseMemberView)
async def update_case_member(
    case_id: UUID,
    user_id: UUID,
    body: CaseMemberUpdateRequest,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_member_manage)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseMemberView:
    try:
        return await case_service.update_case_member(
            repository,
            case_id,
            user_id,
            body,
            changed_by_user_id=principal.principal.user_id,
            now=datetime.now(UTC),
            request_id=get_request_id(),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/{case_id}/members/{user_id}", response_model=CaseMemberView)
async def deactivate_case_member(
    case_id: UUID,
    user_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_member_manage)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> CaseMemberView:
    try:
        return await case_service.deactivate_case_member(
            repository,
            case_id,
            user_id,
            changed_by_user_id=principal.principal.user_id,
            now=datetime.now(UTC),
            request_id=get_request_id(),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{case_id}/notes", response_model=CaseNoteRecord, status_code=status.HTTP_201_CREATED)
async def add_case_note(
    case_id: UUID,
    body: CaseNoteCreateRequest,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_case_note_write)],
    repository: Annotated[CaseNoteRepository, Depends(get_case_note_repository)],
    integrity_service: Annotated[IntegrityService, Depends(get_integrity_service)],
) -> CaseNoteRecord:
    """Append-only: an "edit" is a new note with `supersedes_note_id` set,
    never a mutation of an existing row."""
    return await notes_service.create_note(
        repository,
        integrity_service,
        case_id=case_id,
        author_user_id=principal.principal.user_id,
        text=body.text,
        supersedes_note_id=body.supersedes_note_id,
    )


@router.get("/{case_id}/notes", response_model=CaseNoteListResponse)
async def list_case_notes(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_case_read)],
    repository: Annotated[CaseNoteRepository, Depends(get_case_note_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    cursor: Annotated[
        str | None,
        Query(
            description=(
                "Gap-Closure re-close (G17): an opaque, case-bound, "
                "tamper-evident cursor from a previous response's "
                "`next_cursor`. Takes precedence over `offset` when given."
            )
        ),
    ] = None,
) -> CaseNoteListResponse:
    """Any case member can call this, but the visible set depends on role:
    `CASE_OWNER`/`CASE_MANAGER`/`REVIEWER` see every note; everyone else
    sees only their own (`notes_service.list_visible_notes`). `limit`/
    `offset` (Gap-Closure WP-6, G7/pagination) mirror `queries.
    list_case_observations`'s existing convention -- previously this route
    had no bound at all."""
    signing_key = derive_cursor_signing_key(settings.auth_jwt_secret.get_secret_value())
    after = None
    if cursor is not None:
        try:
            after = decode_cursor(signing_key, cursor, expected_case_id=case_id)
        except CursorError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
    notes = await notes_service.list_visible_notes(
        repository,
        case_id=case_id,
        caller_user_id=principal.principal.user_id,
        caller_role=principal.membership.role,
        limit=limit,
        offset=offset,
        after=after,
    )
    next_cursor = None
    if notes:
        last = notes[-1]
        next_cursor = encode_cursor(
            signing_key,
            CursorPosition(case_id=case_id, created_at=last.created_at, row_id=last.note_id),
        )
    return CaseNoteListResponse(items=tuple(notes), next_cursor=next_cursor)


@router.get("/{case_id}/audit", response_model=CaseAuditEventListResponse)
async def list_case_audit_events(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_case_read)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CaseAuditEventListResponse:
    """Gap-Closure WP-4 (G7): bounded, case-scoped, safe-only -- never a raw payload."""
    events = await repository.list_audit_events_for_case(case_id, limit=limit)
    return CaseAuditEventListResponse(items=tuple(events))


__all__ = ["router"]
