"""Scenarios 12-17: RBAC/ABAC default-deny policy engine."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.access_control.models import ROLE_ACTIONS, CaseAction, CaseRole, ClearanceLevel
from app.modules.access_control.policy import authorize_case_action
from tests.fixtures.access_control.factories import make_case_record, make_membership_record


def _allow(
    *,
    case_id=None,
    action=CaseAction.CASE_READ,
    user_is_active=True,
    role=CaseRole.INVESTIGATOR,
    clearance=ClearanceLevel.CONFIDENTIAL,
    classification=ClearanceLevel.CONFIDENTIAL,
    membership_is_active=True,
    membership_case_id=None,
    case_present=True,
    membership_present=True,
) -> bool:
    real_case_id = case_id if case_id is not None else uuid4()
    case = (
        make_case_record(case_id=real_case_id, classification=classification)
        if case_present
        else None
    )
    membership = (
        make_membership_record(
            case_id=membership_case_id if membership_case_id is not None else real_case_id,
            role=role,
            clearance=clearance,
            is_active=membership_is_active,
        )
        if membership_present
        else None
    )
    return authorize_case_action(
        case_id=real_case_id,
        action=action,
        user_is_active=user_is_active,
        membership=membership,
        case=case,
    )


# --- Scenario 12: role/action matrix ----------------------------------------


def test_case_owner_can_do_everything() -> None:
    for action in CaseAction:
        assert _allow(role=CaseRole.CASE_OWNER, action=action, clearance=ClearanceLevel.SECRET)


def test_viewer_cannot_write_evidence_or_manage_case() -> None:
    assert not _allow(role=CaseRole.VIEWER, action=CaseAction.EVIDENCE_WRITE)
    assert not _allow(role=CaseRole.VIEWER, action=CaseAction.CASE_MANAGE)
    assert not _allow(role=CaseRole.VIEWER, action=CaseAction.REVIEW_DECIDE)


def test_viewer_can_read_case_and_graph() -> None:
    assert _allow(role=CaseRole.VIEWER, action=CaseAction.CASE_READ)
    assert _allow(role=CaseRole.VIEWER, action=CaseAction.GRAPH_READ)


def test_analyst_can_read_but_not_write_evidence() -> None:
    assert _allow(role=CaseRole.ANALYST, action=CaseAction.EVIDENCE_READ)
    assert not _allow(role=CaseRole.ANALYST, action=CaseAction.EVIDENCE_WRITE)


def test_investigator_can_write_evidence() -> None:
    assert _allow(role=CaseRole.INVESTIGATOR, action=CaseAction.EVIDENCE_WRITE)


def test_reviewer_can_decide_but_not_write_evidence() -> None:
    assert _allow(role=CaseRole.REVIEWER, action=CaseAction.REVIEW_DECIDE)
    assert not _allow(role=CaseRole.REVIEWER, action=CaseAction.EVIDENCE_WRITE)


def test_every_role_has_a_defined_action_set() -> None:
    assert set(ROLE_ACTIONS) == set(CaseRole)


# --- Scenario 13: cross-case isolation --------------------------------------


def test_membership_from_one_case_cannot_access_a_different_case() -> None:
    case_a, case_b = uuid4(), uuid4()
    # Membership belongs to case_a; the request is scoped to case_b.
    assert not _allow(case_id=case_b, membership_case_id=case_a)


# --- Scenario 14: insufficient clearance denies despite a valid role -------


def test_insufficient_clearance_denies_despite_owner_role() -> None:
    assert not _allow(
        role=CaseRole.CASE_OWNER,
        clearance=ClearanceLevel.RESTRICTED,
        classification=ClearanceLevel.SECRET,
    )


def test_sufficient_clearance_allows() -> None:
    assert _allow(clearance=ClearanceLevel.SECRET, classification=ClearanceLevel.SECRET)


def test_resource_classification_hook_is_also_enforced() -> None:
    real_case_id = uuid4()
    case = make_case_record(case_id=real_case_id, classification=ClearanceLevel.RESTRICTED)
    membership = make_membership_record(
        case_id=real_case_id, clearance=ClearanceLevel.CONFIDENTIAL, is_active=True
    )
    allowed = authorize_case_action(
        case_id=real_case_id,
        action=CaseAction.EVIDENCE_READ,
        user_is_active=True,
        membership=membership,
        case=case,
        resource_classification=ClearanceLevel.SECRET,
    )
    assert not allowed


# --- Scenario 15: inactive membership/user denies ---------------------------


def test_inactive_membership_denies() -> None:
    assert not _allow(membership_is_active=False)


def test_inactive_user_denies() -> None:
    assert not _allow(user_is_active=False)


# --- Scenario 16: missing case ID denies ------------------------------------


def test_missing_case_id_denies() -> None:
    case = make_case_record()
    membership = make_membership_record(case_id=case.case_id)
    assert not authorize_case_action(
        case_id=None,  # type: ignore[arg-type]
        action=CaseAction.CASE_READ,
        user_is_active=True,
        membership=membership,
        case=case,
    )


# --- Scenario 17: default deny for unknown/missing data ---------------------


def test_missing_case_denies() -> None:
    assert not _allow(case_present=False)


def test_missing_membership_denies() -> None:
    assert not _allow(membership_present=False)


def test_unrecognized_role_denies_via_default_empty_action_set() -> None:
    real_case_id = uuid4()
    case = make_case_record(case_id=real_case_id)
    # Bypass the CaseRole enum entirely -- simulate a membership record
    # whose role somehow isn't one ROLE_ACTIONS recognizes.
    membership = make_membership_record(case_id=real_case_id).model_copy(
        update={"role": "totally-made-up-role"}
    )
    assert not authorize_case_action(
        case_id=real_case_id,
        action=CaseAction.CASE_READ,
        user_is_active=True,
        membership=membership,
        case=case,
    )


@pytest.mark.parametrize("action", list(CaseAction))
def test_no_role_grants_an_action_outside_its_documented_set(action: CaseAction) -> None:
    for role, actions in ROLE_ACTIONS.items():
        expected = action in actions
        assert _allow(role=role, action=action) == expected
