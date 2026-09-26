"""One-time, private-deployment first-Provisioner provisioning.

This is intentionally separate from normal user provisioning: it is usable
only while no active Provisioner exists and only with a deployment
secret supplied out-of-band.  It is not a public registration mechanism.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa

from app.modules.access_control.models import (
    AuditOutcome,
    FirstAdminSetupRequest,
    PublicUser,
    SecurityAuditEventRecord,
    SystemRole,
    UserRecord,
)
from app.modules.access_control.password import hash_password
from app.modules.access_control.repository import AccessControlRepository


def setup_is_available(*, enabled: bool, token: str | None) -> bool:
    """A blank deployment secret never enables the browser setup endpoint."""
    return enabled and token is not None and len(token) >= 32


def token_matches(*, expected: str, submitted: str | None) -> bool:
    """Constant-time comparison; neither value is ever logged or returned."""
    return submitted is not None and hmac.compare_digest(expected, submitted)


async def first_admin_setup_required(
    repository: AccessControlRepository, *, enabled: bool, token: str | None
) -> bool:
    return setup_is_available(enabled=enabled, token=token) and not bool(
        await repository.has_user_with_system_role(SystemRole.PROVISIONER.value)
    )


async def create_first_admin(
    repository: AccessControlRepository,
    request: FirstAdminSetupRequest,
    *,
    now: datetime,
    request_id: str | None,
    ip_marker: str | None,
) -> PublicUser | None:
    """Attempt the serialized one-time creation; ``None`` means setup is closed."""
    user = UserRecord(
        user_id=uuid4(),
        email_normalized=request.email,
        display_name=request.display_name,
        password_hash=hash_password(request.password),
        is_active=True,
        created_at=now,
        updated_at=now,
        system_role=SystemRole.PROVISIONER,
    )
    # Do not record the organization label, setup secret, password, or hash.
    audit_event = SecurityAuditEventRecord(
        event_id=uuid4(),
        occurred_at=now,
        event_type="provisioner.first_setup",
        outcome=AuditOutcome.SUCCESS,
        request_id=request_id,
        user_id_nullable=user.user_id,
        case_id_nullable=None,
        ip_hash_or_safe_network_marker=ip_marker,
        metadata_safe_json={"provisioning_path": "private_first_provisioner_setup"},
    )
    try:
        created = await repository.create_first_provisioner_if_none(user, audit_event)
    except sa.exc.IntegrityError:
        # Duplicate email/race details are deliberately not exposed on the
        # unauthenticated setup surface.
        return None
    if not created:
        return None
    return PublicUser(
        user_id=user.user_id,
        email_normalized=user.email_normalized,
        display_name=user.display_name,
        is_active=user.is_active,
        created_at=user.created_at,
        system_role=user.system_role,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
    )


__all__ = [
    "create_first_admin",
    "first_admin_setup_required",
    "setup_is_available",
    "token_matches",
]
