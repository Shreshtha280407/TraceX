"""Case-management orchestration: create a case, add a member, read status.

Mirrors `service.py`'s split from `api.py` -- no FastAPI/HTTP concerns live
here. `repository.create_case`/`create_membership` already existed (Phase 1
access-control foundation); this module is the first caller that reaches
them from an HTTP route (G6) rather than only from test fixtures.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa

from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.errors import ValidationError
from app.modules.access_control.models import (
    AuditOutcome,
    CaseCreateRequest,
    CaseMemberAddRequest,
    CaseMembershipRecord,
    CaseMemberView,
    CaseRecord,
    CaseRole,
    CaseStatus,
    CaseStatusView,
    CaseView,
)
from app.modules.access_control.repository import AccessControlRepository


def _case_view(case: CaseRecord) -> CaseView:
    return CaseView(
        case_id=case.case_id,
        case_reference=case.case_reference,
        classification=case.classification,
        status=case.status,
        created_at=case.created_at,
    )


def _member_view(membership: CaseMembershipRecord) -> CaseMemberView:
    return CaseMemberView(
        user_id=membership.user_id,
        role=membership.role,
        clearance=membership.clearance,
        is_active=membership.is_active,
    )


async def create_case(
    repository: AccessControlRepository,
    request: CaseCreateRequest,
    *,
    creator_user_id: UUID,
    now: datetime,
    request_id: str | None,
) -> CaseView:
    """Create a case and make the creator its `CASE_OWNER`.

    The owner's own clearance is set to exactly the case's classification
    (never higher) -- enough to satisfy `policy.authorize_case_action`'s
    clearance check immediately, without presuming the creator should hold
    more clearance than the case they just defined.
    """
    case = CaseRecord(
        case_id=uuid4(),
        case_reference=request.case_reference,
        classification=request.classification,
        status=CaseStatus.OPEN,
        created_at=now,
    )
    try:
        await repository.create_case(case)
    except sa.exc.IntegrityError as exc:
        raise ValidationError("case reference already exists") from exc

    membership = CaseMembershipRecord(
        membership_id=uuid4(),
        case_id=case.case_id,
        user_id=creator_user_id,
        role=CaseRole.CASE_OWNER,
        clearance=request.classification,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    await repository.create_membership(membership)

    await record_audit_event(
        repository,
        event_type="case.create",
        outcome=AuditOutcome.SUCCESS,
        now=now,
        request_id=request_id,
        user_id=creator_user_id,
        case_id=case.case_id,
        metadata={"classification": request.classification.value},
    )
    return _case_view(case)


async def get_case_view(repository: AccessControlRepository, case_id: UUID) -> CaseView | None:
    case = await repository.get_case(case_id)
    return _case_view(case) if case is not None else None


async def get_case_status(
    repository: AccessControlRepository, case_id: UUID
) -> CaseStatusView | None:
    case = await repository.get_case(case_id)
    return CaseStatusView(case_id=case.case_id, status=case.status) if case is not None else None


async def add_case_member(
    repository: AccessControlRepository,
    case_id: UUID,
    request: CaseMemberAddRequest,
    *,
    added_by_user_id: UUID,
    now: datetime,
    request_id: str | None,
) -> CaseMemberView:
    """Add an active member to an existing case. Never reactivates a removed member implicitly.

    Raises `ValidationError` if the target user doesn't exist or already
    has an active membership on this case -- membership changes for an
    existing member are out of this WP's scope (no update/remove route
    exists yet; see `docs/qa/known-limitations.md`).
    """
    target_user = await repository.get_user_by_id(request.user_id)
    if target_user is None:
        raise ValidationError("target user does not exist")

    existing = await repository.get_active_membership(case_id, request.user_id)
    if existing is not None:
        raise ValidationError("user already has an active membership on this case")

    membership = CaseMembershipRecord(
        membership_id=uuid4(),
        case_id=case_id,
        user_id=request.user_id,
        role=request.role,
        clearance=request.clearance,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    await repository.create_membership(membership)

    await record_audit_event(
        repository,
        event_type="case.member_add",
        outcome=AuditOutcome.SUCCESS,
        now=now,
        request_id=request_id,
        user_id=added_by_user_id,
        case_id=case_id,
        metadata={"added_user_id": str(request.user_id), "role": request.role.value},
    )
    return _member_view(membership)


__all__ = ["add_case_member", "create_case", "get_case_status", "get_case_view"]
