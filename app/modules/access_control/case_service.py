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
    CaseMemberDetailView,
    CaseMembershipRecord,
    CaseMemberUpdateRequest,
    CaseMemberView,
    CaseRecord,
    CaseRole,
    CaseStatus,
    CaseStatusView,
    CaseTeamMemberCreateRequest,
    CaseView,
    ClearanceLevel,
    SecurityAuditEventRecord,
    UserRecord,
    clearance_satisfies,
)
from app.modules.access_control.password import hash_password
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
    try:
        await repository.create_case_with_owner(case, membership)
    except sa.exc.IntegrityError as exc:
        raise ValidationError("case reference already exists") from exc

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
    actor_membership: CaseMembershipRecord,
    now: datetime,
    request_id: str | None,
) -> CaseMemberView:
    """Assign an active user with an explicit case role and clearance."""
    _ensure_member_change_allowed(
        actor_membership, requested_role=request.role, requested_clearance=request.clearance
    )
    target_user = await repository.get_user_by_id(request.user_id)
    if target_user is None:
        raise ValidationError("target user does not exist")
    if not target_user.is_active:
        raise ValidationError("target user is inactive")
    if target_user.system_role is not None:
        raise ValidationError("organisation-role accounts cannot be assigned as team members")

    existing = await repository.get_active_membership(case_id, request.user_id)
    if existing is not None:
        raise ValidationError("user already has an active membership on this case")

    # The schema has one membership row per (case, user). Reassignment of a
    # deliberately deactivated account is explicit here, with fresh role and
    # clearance selected by the manager; it never happens as a side effect of
    # a login or unrelated account action.
    inactive = next(
        (
            membership
            for membership, _user in await repository.list_case_members(case_id)
            if membership.user_id == request.user_id and not membership.is_active
        ),
        None,
    )
    if inactive is not None:
        _ensure_member_change_allowed(
            actor_membership,
            requested_role=request.role,
            requested_clearance=request.clearance,
            target_role=inactive.role,
        )
        restored = await repository.update_case_membership(
            case_id,
            request.user_id,
            role=request.role.value,
            clearance=request.clearance.value,
            is_active=True,
            updated_at=now,
        )
        if restored is None:  # defensive: the row was concurrently changed
            raise ValidationError("case membership could not be restored")
        await record_audit_event(
            repository,
            event_type="case.member_update",
            outcome=AuditOutcome.SUCCESS,
            now=now,
            request_id=request_id,
            user_id=added_by_user_id,
            case_id=case_id,
            metadata={"added_user_id": str(request.user_id), "role": request.role.value},
        )
        return _member_view(restored)

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


async def list_case_members(
    repository: AccessControlRepository, case_id: UUID
) -> list[CaseMemberDetailView]:
    """Case-scoped directory for a caller already authorized to manage members."""
    rows = await repository.list_case_members(case_id)
    return [
        CaseMemberDetailView(
            user_id=membership.user_id,
            display_name=user.display_name,
            email_normalized=user.email_normalized,
            role=membership.role,
            clearance=membership.clearance,
            is_active=membership.is_active,
        )
        for membership, user in rows
    ]


async def update_case_member(
    repository: AccessControlRepository,
    case_id: UUID,
    user_id: UUID,
    request: CaseMemberUpdateRequest,
    *,
    changed_by_user_id: UUID,
    actor_membership: CaseMembershipRecord,
    now: datetime,
    request_id: str | None,
    is_active: bool = True,
) -> CaseMemberView:
    current = await repository.get_active_membership(case_id, user_id)
    if current is None:
        raise ValidationError("active case membership not found")
    target_user = await repository.get_user_by_id(user_id)
    if target_user is None or (
        target_user.system_role is not None and target_user.user_id != changed_by_user_id
    ):
        raise ValidationError("protected account cannot be managed through a case")
    _ensure_member_change_allowed(
        actor_membership,
        requested_role=request.role,
        requested_clearance=request.clearance,
        target_role=current.role,
    )
    try:
        membership = await repository.update_case_membership(
            case_id,
            user_id,
            role=request.role.value,
            clearance=request.clearance.value,
            is_active=is_active,
            updated_at=now,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if membership is None:
        raise ValidationError("active case membership not found")
    if is_active:
        await record_audit_event(
            repository,
            event_type="case.member_update",
            outcome=AuditOutcome.SUCCESS,
            now=now,
            request_id=request_id,
            user_id=changed_by_user_id,
            case_id=case_id,
            metadata={"changed_user_id": str(user_id), "role": request.role.value},
        )
    else:
        await record_audit_event(
            repository,
            event_type="case.member_deactivate",
            outcome=AuditOutcome.SUCCESS,
            now=now,
            request_id=request_id,
            user_id=changed_by_user_id,
            case_id=case_id,
            metadata={"changed_user_id": str(user_id), "role": request.role.value},
        )
    return _member_view(membership)


async def deactivate_case_member(
    repository: AccessControlRepository,
    case_id: UUID,
    user_id: UUID,
    *,
    changed_by_user_id: UUID,
    actor_membership: CaseMembershipRecord,
    now: datetime,
    request_id: str | None,
) -> CaseMemberView:
    current = await repository.get_active_membership(case_id, user_id)
    if current is None:
        raise ValidationError("active case membership not found")
    target_user = await repository.get_user_by_id(user_id)
    if target_user is None or (
        target_user.system_role is not None and target_user.user_id != changed_by_user_id
    ):
        raise ValidationError("protected account cannot be managed through a case")
    _ensure_member_change_allowed(
        actor_membership,
        requested_role=current.role,
        requested_clearance=current.clearance,
        target_role=current.role,
    )
    return await update_case_member(
        repository,
        case_id,
        user_id,
        CaseMemberUpdateRequest(role=current.role, clearance=current.clearance),
        changed_by_user_id=changed_by_user_id,
        actor_membership=actor_membership,
        now=now,
        request_id=request_id,
        is_active=False,
    )


def _ensure_member_change_allowed(
    actor_membership: CaseMembershipRecord,
    *,
    requested_role: CaseRole,
    requested_clearance: ClearanceLevel,
    target_role: CaseRole | None = None,
) -> None:
    """Apply the deliberately narrow owner/manager delegation policy.

    Owners and managers may never grant clearance above their own.  Managers
    may manage only investigator/analyst/reviewer/viewer memberships; only
    owners can create, alter, or deactivate owner/manager-level memberships.
    This is enforced here as well as in the route dependency so crafted HTTP
    input cannot promote a manager or bypass the UI.
    """
    if actor_membership.role not in {CaseRole.CASE_OWNER, CaseRole.CASE_MANAGER}:
        raise ValidationError("case member management is not permitted")
    if not clearance_satisfies(actor_membership.clearance, requested_clearance):
        raise ValidationError("cannot grant clearance above your own")
    privileged_roles = {CaseRole.CASE_OWNER, CaseRole.CASE_MANAGER}
    if actor_membership.role == CaseRole.CASE_MANAGER and (
        requested_role in privileged_roles or target_role in privileged_roles
    ):
        raise ValidationError("only a case owner can manage owner or manager memberships")


async def create_case_team_member(
    repository: AccessControlRepository,
    case_id: UUID,
    request: CaseTeamMemberCreateRequest,
    *,
    added_by_user_id: UUID,
    actor_membership: CaseMembershipRecord,
    now: datetime,
    request_id: str | None,
) -> CaseMemberView:
    """Create an ordinary user and grant access only to the selected case."""
    _ensure_member_change_allowed(
        actor_membership, requested_role=request.role, requested_clearance=request.clearance
    )
    if request.role == CaseRole.CASE_OWNER:
        raise ValidationError("create a team member with a non-owner case role")
    if await repository.get_user_by_email(request.email) is not None:
        raise ValidationError("email already registered")
    user = UserRecord(
        user_id=uuid4(),
        email_normalized=request.email,
        display_name=request.display_name,
        password_hash=hash_password(request.password),
        is_active=True,
        created_at=now,
        updated_at=now,
        system_role=None,
        must_change_password=True,
    )
    membership = CaseMembershipRecord(
        membership_id=uuid4(),
        case_id=case_id,
        user_id=user.user_id,
        role=request.role,
        clearance=request.clearance,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    audit_event = SecurityAuditEventRecord(
        event_id=uuid4(),
        occurred_at=now,
        event_type="case.create_team_member",
        outcome=AuditOutcome.SUCCESS,
        request_id=request_id,
        user_id_nullable=added_by_user_id,
        case_id_nullable=case_id,
        ip_hash_or_safe_network_marker=None,
        metadata_safe_json={"created_user_id": str(user.user_id), "role": request.role.value},
    )
    try:
        await repository.create_team_user_with_membership(user, membership, audit_event)
    except sa.exc.IntegrityError as exc:
        raise ValidationError("email already registered") from exc
    return _member_view(membership)


async def authorize_team_member_credential_reset(
    repository: AccessControlRepository,
    case_id: UUID,
    user_id: UUID,
    *,
    actor_membership: CaseMembershipRecord,
) -> None:
    """Prove that a credential reset targets an ordinary member of this case.

    The API calls this before changing a password or TOTP record.  Therefore
    a crafted case route can never reset, deactivate, or inspect a
    Provisioner (or any other organisation-level account).
    """
    membership = await repository.get_active_membership(case_id, user_id)
    target_user = await repository.get_user_by_id(user_id)
    if membership is None or target_user is None or target_user.system_role is not None:
        raise ValidationError("team member not found")
    _ensure_member_change_allowed(
        actor_membership,
        requested_role=membership.role,
        requested_clearance=membership.clearance,
        target_role=membership.role,
    )


__all__ = [
    "add_case_member",
    "authorize_team_member_credential_reset",
    "create_case_team_member",
    "create_case",
    "deactivate_case_member",
    "get_case_status",
    "get_case_view",
    "list_case_members",
    "update_case_member",
]
