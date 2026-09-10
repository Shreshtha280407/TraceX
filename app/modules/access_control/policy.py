"""Case-scoped RBAC/ABAC policy engine. Default deny.

`authorize_case_action` is the single decision point every case-resource
dependency (`dependencies.py`) goes through. It never trusts a
caller-supplied role/clearance value -- only a `CaseMembershipRecord`
already loaded from `case_memberships` by `repository.py` -- and it denies
whenever anything it needs is missing, inactive, mismatched, or
unrecognized, rather than falling through to an implicit allow. See
`docs/architecture/access-control-v1.md` for the human-readable role/action
and clearance/classification matrices this module encodes.
"""

from __future__ import annotations

from uuid import UUID

from app.modules.access_control.models import (
    ROLE_ACTIONS,
    CaseAction,
    CaseMembershipRecord,
    CaseRecord,
    ClearanceLevel,
    clearance_satisfies,
)


def authorize_case_action(
    *,
    case_id: UUID,
    action: CaseAction,
    user_is_active: bool,
    membership: CaseMembershipRecord | None,
    case: CaseRecord | None,
    resource_classification: ClearanceLevel | None = None,
) -> bool:
    """True only if every required check passes; default deny otherwise.

    Required checks, all of which must hold (this function accepts
    `case_id` explicitly and re-checks it against both `case` and
    `membership` -- it never trusts that a caller looked either of those up
    against the right ID, and it never relies on any notion of a globally
    selected "current case"):

    1. `case_id` was actually supplied (not falsy/`None`).
    2. `user_is_active` -- the authenticated user's account is active.
    3. `case` is not `None` and `case.case_id == case_id` -- the case exists.
    4. `membership` is not `None` and `membership.case_id == case_id` --
       an active membership exists for this *exact* case.
    5. `membership.is_active` -- the membership itself hasn't been
       deactivated.
    6. `action in ROLE_ACTIONS[membership.role]` -- the role permits the
       requested action. An unrecognized role denies via a `dict.get(...,
       frozenset())` default rather than raising: default-deny extends to
       "role we don't recognize", not just "role missing" (this should be
       unreachable given the database's `CHECK` constraint on `role`, but
       is never trusted blindly here regardless).
    7. `membership.clearance` ranks at or above `case.classification`.
    8. If `resource_classification` is supplied (a later-phase hook for
       per-evidence/per-artifact classification, once one exists),
       `membership.clearance` must rank at or above it too.
    """
    if not case_id:
        return False
    if not user_is_active:
        return False
    if case is None or case.case_id != case_id:
        return False
    if membership is None or membership.case_id != case_id:
        return False
    if not membership.is_active:
        return False
    if action not in ROLE_ACTIONS.get(membership.role, frozenset()):
        return False
    if not clearance_satisfies(membership.clearance, case.classification):
        return False
    return resource_classification is None or clearance_satisfies(
        membership.clearance, resource_classification
    )
