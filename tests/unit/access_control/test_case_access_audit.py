"""Scenario 14: denied case actions produce safe audit events.

Closes the documented `require_case_action` audit gap (see
`docs/architecture/security-boundaries-v1.md`'s "Integration Hardening 1"
history and `docs/architecture/phase-2-decisions.md`'s "Open questions").
Calls the dependency function directly -- `Depends(...)` is just FastAPI
metadata, so the underlying async function can be invoked with plain
Python objects, no HTTP stack needed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.modules.access_control.dependencies import require_case_action
from app.modules.access_control.models import (
    AuditOutcome,
    AuthenticatedPrincipal,
    CaseAction,
    CaseRole,
)
from tests.fixtures.access_control.factories import make_case_record, make_membership_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository


async def test_denied_case_action_records_a_safe_audit_event() -> None:
    repository = FakeAccessControlRepository()
    case = make_case_record()
    await repository.create_case(case)
    # Deliberately no membership created -- this is the denial this test targets.
    principal = AuthenticatedPrincipal(user_id=uuid4(), session_id=uuid4())
    dependency = require_case_action(CaseAction.EVIDENCE_READ)

    with pytest.raises(HTTPException) as excinfo:
        await dependency(case_id=case.case_id, principal=principal, repository=repository)
    assert excinfo.value.status_code == 403

    assert len(repository.audit_events) == 1
    event = repository.audit_events[0]
    assert event.event_type == "case_access_denied"
    assert event.outcome is AuditOutcome.DENIED
    assert event.user_id_nullable == principal.user_id
    assert event.case_id_nullable == case.case_id
    assert event.metadata_safe_json == {"action": "evidence_read"}


async def test_denied_case_action_audit_never_reveals_which_check_failed() -> None:
    """Insufficient clearance and no membership at all must audit identically.

    The *client-facing* response is already required to be indistinguishable
    (default-deny); the audit trail's `metadata` must not smuggle that
    distinction back out either -- both denials carry only the attempted
    action, never a reason like "insufficient_clearance"/"no_membership".
    """
    repository = FakeAccessControlRepository()
    case = make_case_record()
    await repository.create_case(case)
    principal = AuthenticatedPrincipal(user_id=uuid4(), session_id=uuid4())
    dependency = require_case_action(CaseAction.EVIDENCE_READ)

    with pytest.raises(HTTPException):
        await dependency(case_id=case.case_id, principal=principal, repository=repository)

    # CaseRole.VIEWER is a real, active membership -- just one without
    # EVIDENCE_READ permission (see ROLE_ACTIONS) -- a different failure
    # reason than "no membership at all" above.
    membership = make_membership_record(
        case_id=case.case_id, user_id=principal.user_id, role=CaseRole.VIEWER
    )
    await repository.create_membership(membership)
    with pytest.raises(HTTPException):
        await dependency(case_id=case.case_id, principal=principal, repository=repository)

    assert len(repository.audit_events) == 2
    assert (
        repository.audit_events[0].metadata_safe_json
        == repository.audit_events[1].metadata_safe_json
    )


async def test_successful_case_action_records_only_a_safe_grant_audit_event() -> None:
    repository = FakeAccessControlRepository()
    case = make_case_record()
    await repository.create_case(case)
    principal = AuthenticatedPrincipal(user_id=uuid4(), session_id=uuid4())
    membership = make_membership_record(case_id=case.case_id, user_id=principal.user_id)
    await repository.create_membership(membership)
    dependency = require_case_action(CaseAction.EVIDENCE_READ)

    result = await dependency(case_id=case.case_id, principal=principal, repository=repository)

    assert result.case_id == case.case_id
    assert result.action is CaseAction.EVIDENCE_READ
    assert result.allowed is True
    assert len(repository.audit_events) == 1
    event = repository.audit_events[0]
    assert event.event_type == "case_access_granted"
    assert event.metadata_safe_json == {"action": "evidence_read"}
    assert event.user_id_nullable == principal.user_id
    assert event.case_id_nullable == case.case_id


async def test_audit_write_failure_never_turns_a_deny_into_a_grant() -> None:
    class _BrokenRepository(FakeAccessControlRepository):
        async def record_audit_event(self, event: object) -> None:  # type: ignore[override]
            raise RuntimeError("simulated audit-sink outage")

    repository = _BrokenRepository()
    case = make_case_record()
    await repository.create_case(case)
    principal = AuthenticatedPrincipal(user_id=uuid4(), session_id=uuid4())
    dependency = require_case_action(CaseAction.EVIDENCE_READ)

    # The audit sink is broken, but the request must still be denied --
    # never silently granted, and never actually raise the RuntimeError
    # (record_audit_event_safely swallows it).
    with pytest.raises(HTTPException) as excinfo:
        await dependency(case_id=case.case_id, principal=principal, repository=repository)
    assert excinfo.value.status_code == 403


async def test_case_authorization_database_outage_is_a_safe_service_error() -> None:
    class _UnavailableRepository(FakeAccessControlRepository):
        async def get_active_membership(self, case_id, user_id):  # type: ignore[override]
            raise sa.exc.OperationalError(
                "SELECT memberships", {}, RuntimeError("postgresql://user:secret@unavailable")
            )

    repository = _UnavailableRepository()
    principal = AuthenticatedPrincipal(user_id=uuid4(), session_id=uuid4())
    dependency = require_case_action(CaseAction.EVIDENCE_READ)

    with pytest.raises(HTTPException) as excinfo:
        await dependency(case_id=uuid4(), principal=principal, repository=repository)

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "authorization service temporarily unavailable"
