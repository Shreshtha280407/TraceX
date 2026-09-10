"""Scenario 5: case membership and clearance decisions use live persisted records."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa

from app.modules.access_control.models import (
    CaseAction,
    CaseMembershipRecord,
    CaseRecord,
    CaseRole,
    CaseStatus,
    ClearanceLevel,
    UserRecord,
)
from app.modules.access_control.password import hash_password
from app.modules.access_control.policy import authorize_case_action
from app.modules.access_control.repository import AccessControlRepository
from tests.integration.access_control.conftest import unique_email


async def _seed_case_and_membership(
    repository: AccessControlRepository,
    *,
    classification: ClearanceLevel,
    role: CaseRole,
    clearance: ClearanceLevel,
    cleanup_user_ids: list[UUID],
) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    case_id, user_id = uuid4(), uuid4()
    case = CaseRecord(
        case_id=case_id,
        case_reference=f"ITEST-{uuid4().hex[:8]}",
        classification=classification,
        status=CaseStatus.OPEN,
        created_at=now,
    )
    await repository.create_case(case)
    # case_memberships.user_id carries a foreign key onto users.user_id --
    # a real user row is required, not just a random UUID.
    user = UserRecord(
        user_id=user_id,
        email_normalized=unique_email("policylive"),
        display_name="Policy Live Test",
        password_hash=hash_password("irrelevant-for-this-test-1"),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    await repository.create_user(user)
    cleanup_user_ids.append(user_id)
    membership = CaseMembershipRecord(
        membership_id=uuid4(),
        case_id=case_id,
        user_id=user_id,
        role=role,
        clearance=clearance,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    await repository.create_membership(membership)
    return case_id, user_id


async def test_policy_decision_reflects_live_membership_and_case_rows(
    repository: AccessControlRepository,
    cleanup_case_ids: list[UUID],
    cleanup_user_ids: list[UUID],
) -> None:
    case_id, user_id = await _seed_case_and_membership(
        repository,
        classification=ClearanceLevel.CONFIDENTIAL,
        role=CaseRole.ANALYST,
        clearance=ClearanceLevel.CONFIDENTIAL,
        cleanup_user_ids=cleanup_user_ids,
    )
    cleanup_case_ids.append(case_id)

    loaded_case = await repository.get_case(case_id)
    loaded_membership = await repository.get_active_membership(case_id, user_id)

    assert authorize_case_action(
        case_id=case_id,
        action=CaseAction.EVIDENCE_READ,
        user_is_active=True,
        membership=loaded_membership,
        case=loaded_case,
    )
    # An analyst cannot write evidence -- this must hold using the row
    # actually read back from PostgreSQL, not an in-memory assumption.
    assert not authorize_case_action(
        case_id=case_id,
        action=CaseAction.EVIDENCE_WRITE,
        user_is_active=True,
        membership=loaded_membership,
        case=loaded_case,
    )


async def test_insufficient_clearance_denies_using_live_rows(
    repository: AccessControlRepository,
    cleanup_case_ids: list[UUID],
    cleanup_user_ids: list[UUID],
) -> None:
    case_id, user_id = await _seed_case_and_membership(
        repository,
        classification=ClearanceLevel.SECRET,
        role=CaseRole.CASE_OWNER,
        clearance=ClearanceLevel.RESTRICTED,
        cleanup_user_ids=cleanup_user_ids,
    )
    cleanup_case_ids.append(case_id)

    loaded_case = await repository.get_case(case_id)
    loaded_membership = await repository.get_active_membership(case_id, user_id)
    assert not authorize_case_action(
        case_id=case_id,
        action=CaseAction.CASE_READ,
        user_is_active=True,
        membership=loaded_membership,
        case=loaded_case,
    )


async def test_deactivating_a_membership_row_live_immediately_denies(
    repository: AccessControlRepository,
    cleanup_case_ids: list[UUID],
    cleanup_user_ids: list[UUID],
) -> None:
    case_id, user_id = await _seed_case_and_membership(
        repository,
        classification=ClearanceLevel.RESTRICTED,
        role=CaseRole.INVESTIGATOR,
        clearance=ClearanceLevel.RESTRICTED,
        cleanup_user_ids=cleanup_user_ids,
    )
    cleanup_case_ids.append(case_id)

    assert await repository.get_active_membership(case_id, user_id) is not None

    # Flip the membership inactive directly at the database level -- this
    # is what a future admin/revocation endpoint would do -- and confirm
    # the *next* read (not a cached object) reflects it immediately.
    async with repository._engine.begin() as conn:  # noqa: SLF001 - test-only direct update
        await conn.execute(
            sa.text(
                "UPDATE case_memberships SET is_active = false "
                "WHERE case_id = :case_id AND user_id = :user_id"
            ),
            {"case_id": case_id, "user_id": user_id},
        )

    membership_after = await repository.get_active_membership(case_id, user_id)
    assert membership_after is None

    case = await repository.get_case(case_id)
    assert not authorize_case_action(
        case_id=case_id,
        action=CaseAction.CASE_READ,
        user_is_active=True,
        membership=membership_after,
        case=case,
    )
