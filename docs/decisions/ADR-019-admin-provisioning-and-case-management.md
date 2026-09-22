# ADR-019: Admin-only user provisioning and HTTP case management (Gap-Closure WP-1)

Status: **accepted**. Closes gap register G5 (open self-registration) and G6
(no HTTP case management; unused per-evidence ABAC hook).

## Context

Phase 1-7 shipped `POST /api/v1/auth/register` as an unauthenticated,
publicly reachable route: any caller could create an account with no
admin approval and no audit trail beyond the account-creation event
itself. The MVP plan calls for admin-provisioned accounts only. Separately,
`AccessControlRepository.create_case`/`create_membership` existed from
Phase 1 but were never reachable over HTTP — only test fixtures called
them directly — so there was no way to create a case or add a member
through the API at all.

## Decision 1: no public self-registration; a minimal `system_role` column

`POST /api/v1/auth/register` is removed outright (a 404, not merely
gated) rather than kept and admin-gated under the same path — a genuinely
new admin-only route makes the change in surface area unambiguous instead
of leaving a same-URL route whose behavior silently changed underneath
existing callers/documentation.

The smallest additive model that supports "is this user allowed to
provision other users" is one nullable `users.system_role` column
(`NULL` or `'admin'`, `CHECK` constrained), not a new roles table — Phase 1
already has no other deployment-wide (non-case-scoped) permission concept,
and a single admin/non-admin bit is exactly what provisioning needs. A
`system_role` StrEnum type keeps room to add more values later without a
migration shaped differently than it would need to be anyway.

`POST /api/v1/admin/users` (`app/modules/access_control/api.py`,
`admin_router`) is the only account-creation path. It requires
`require_system_admin` — a new dependency, deliberately separate from
`require_case_action`, since provisioning is not case-scoped and has no
`case_id` to authorize against. Every provisioning call is audited
(`admin.provision_user`, carrying the acting admin's `user_id`).

First-admin bootstrap cannot go through the now-admin-only HTTP route (no
admin exists yet to call it), so `app/modules/access_control/cli.py`
provides `create-admin`: reads the password via `TRACEX_ADMIN_BOOTSTRAP_PASSWORD`
or an interactive `getpass` prompt (never argv, never a log line), checks
`count_users_with_system_role("admin")` for idempotency, and refuses a
second admin unless `--force` is passed. It prints only the created
user's ID.

## Decision 2: case management routes reuse the existing repository methods and role matrix

`app/modules/access_control/cases_api.py` adds `POST /api/v1/cases`,
`GET /api/v1/cases/{id}`, `GET /api/v1/cases/{id}/status`, and
`POST /api/v1/cases/{id}/members`, sharing the `/api/v1/cases` prefix
with `evidence_lifecycle`/`graph`/`integrity`'s existing routers (the
same multi-router-same-prefix pattern those three already use). Case
creation is authenticated-only (`require_authenticated_user`), not
case-scoped, since the case doesn't exist yet to authorize against — the
creator becomes `CASE_OWNER` with clearance set to exactly the case's own
classification. A new `CaseAction.MEMBER_MANAGE` value, added only to
`CASE_OWNER` (already implicit via `frozenset(CaseAction)`) and
`CASE_MANAGER`, gates member addition — matching the plan's "only
CASE_OWNER/MANAGER manage members."

## Decision 3: the per-evidence ABAC hook is wired, not newly added

`policy.authorize_case_action`'s `resource_classification` parameter
already existed but no caller ever supplied a non-`None` value. Evidence
records already carry their own `classification: EvidenceClassification`
field (`app/contracts/evidence.py`, frozen V1) — a *superset* of
`ClearanceLevel` (it adds `UNCLASSIFIED`). Rather than add a matching
`UNCLASSIFIED` value to `ClearanceLevel` (touching the frozen
`app/modules/access_control/models.py` clearance semantics for every
existing caller), `evidence_lifecycle/api.py` maps
`EvidenceClassification -> ClearanceLevel | None` locally
(`UNCLASSIFIED -> None`, meaning "no additional requirement beyond
ordinary case-level `EVIDENCE_READ`") and calls `clearance_satisfies`
directly. `GET .../evidence/{id}` 403s when the caller's clearance is
insufficient; `GET .../evidence` filters such items out of the list
silently — a list must not reveal that higher-classified evidence exists
at all, not just refuse to open it.

## Alternatives considered

- **Keep `/register` open but require email verification.** Rejected:
  the plan calls for admin-provisioned accounts specifically, not merely
  spam-resistant ones; nothing about email verification satisfies "no
  public signup."
- **A full RBAC roles table for system-level permissions.** Rejected as
  premature: exactly one system capability (`admin`) exists today: a
  richer table can be introduced additively later if a second one
  appears, without migrating this column.
- **Add `UNCLASSIFIED` to `ClearanceLevel` instead of mapping locally.**
  Rejected: `ClearanceLevel` is a case-membership clearance concept used
  everywhere in `policy.py`; conflating it with evidence's own (frozen,
  wider) classification enum would let an `UNCLASSIFIED` *membership
  clearance* exist, which was never a real concept a case membership
  should have.
