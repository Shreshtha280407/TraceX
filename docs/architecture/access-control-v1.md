# Access Control v1

Owner: Aditya. Status: foundation for authentication, session security, and case-scoped RBAC/ABAC, built in `app/modules/access_control/`. See `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md` for the reasoning behind each non-obvious choice below, and `docs/architecture/security-boundaries-v1.md` for the security-boundary/middleware picture.

**This document does not modify any frozen `V1` contract in `app/contracts/`.** Everything here is a new, self-contained module built against `app/core/config.py`, PostgreSQL, and Redis.

## Module map

```text
app/modules/access_control/
├── __init__.py
├── models.py       # enums, role/action matrix, all request/response/record models
├── errors.py       # typed errors, never constructed with a secret in the message
├── password.py     # Argon2id hashing + the MVP password policy
├── tokens.py       # signed JWT access tokens + opaque refresh-token secrets
├── sessions.py      # refresh-token lifecycle: create, rotate, reuse detection, revoke
├── repository.py    # the only module that touches SQLAlchemy/PostgreSQL directly
├── service.py        # register/login/refresh/logout/me orchestration
├── policy.py          # case-scoped RBAC/ABAC, default deny
├── dependencies.py     # FastAPI dependencies: require_authenticated_user, require_case_action, ...
├── rate_limit.py         # fixed-window login/refresh limiter (Redis + in-memory)
├── audit.py               # safe security-audit-event recording
└── retry.py                # bounded retry for explicitly idempotent, transient-failure-prone work
```

Dependency direction is one-way: `api.py`/`dependencies.py` → `service.py` → `sessions.py`/`password.py`/`tokens.py`/`audit.py`/`rate_limit.py` → `repository.py`/`policy.py` → nothing else in this module. `repository.py` is the only file that imports SQLAlchemy/PostgreSQL; `rate_limit.py` is the only one that imports `redis`. Nothing here imports `app/modules/graph/` or `app/modules/structured_processing/` (checked statically in `tests/security/access_control/test_module_boundaries.py`).

## Authentication flow

```text
POST /api/v1/auth/register         -> PublicUser (201)
POST /api/v1/auth/login            -> TokenPairResponse (200) | 401 generic | 429
POST /api/v1/auth/refresh          -> TokenPairResponse (200) | 401 generic | 429
POST /api/v1/auth/logout           -> 204 (idempotent)
GET  /api/v1/auth/me               -> MeResponse (200) | 401
POST /api/v1/auth/mfa/enroll       -> MfaEnrollResponse (200) | 401
POST /api/v1/auth/mfa/verify       -> PublicUser (200) | 401 | 409
POST /api/v1/auth/mfa/login-verify -> TokenPairResponse (200) | 401 generic | 429
POST /api/v1/auth/change-password  -> 204 | 401
```

See "Multi-factor authentication (TOTP) and forced credential ceremony" below for the last four.

- **Register**: validates email format (hand-rolled regex + normalization — see below; `pydantic.EmailStr` needs `email-validator`, not an approved dependency this phase) and the password policy (`password.MIN_PASSWORD_LENGTH` = 10, `MAX_PASSWORD_LENGTH` = 256), hashes the password with Argon2id, stores the user with `is_active=True` and **no role/privilege field at all** — there is nothing to elevate, since privilege only ever comes from an explicit `case_memberships` row created later, out of band. Duplicate email returns `409 Conflict` (not a generic anti-enumeration denial — see ADR-003 for why register and login differ here).
- **Login**: rate-limited first (`rate_limit.py`), then looks up the user and verifies the password. Unknown email, inactive user, and wrong password all raise the identical `AuthenticationError` → identical `401` body — and an unknown/inactive-user attempt still runs a real Argon2id verification against a fixed dummy hash, so response *timing* can't distinguish the three cases either (see `service._DUMMY_PASSWORD_HASH`). On success, creates a brand-new session (`sessions.create_session`) and a short-lived access token.
- **Refresh**: rate-limited first, then rotates the refresh token (`sessions.rotate_session`, see below). A denial (unknown token, expired, already revoked, or reuse of a rotated-away token) always renders as the same generic `401`.
- **Logout**: revokes the session behind the given refresh token. Unknown or already-revoked tokens are treated as "already logged out" — never an error, so repeated logout is safe.
- **Me**: requires a valid access token (`require_authenticated_user`); returns the public user plus every *active* case membership. Never returns a password hash, session ID, or audit data.

## Token design

```text
access token: short-lived signed JWT (HS256 by default)
refresh token: opaque random secret (256 bits), SHA-256-hashed before storage
```

Access-token claims are exactly:

```text
sub  user_id (UUID)
sid  session_id (UUID) -- a live-session reference, not a permission
iat  issued-at
exp  expiry
iss  AUTH_JWT_ISSUER
aud  AUTH_JWT_AUDIENCE
typ  "access_v1" -- a token type/version tag
```

No case membership, role, clearance, or any other frequently-changing authorization fact is embedded in the token. Case access is always evaluated against `case_memberships`/`cases` rows read live at request time (`dependencies.require_case_action`), so revoking a membership takes effect on the very next request — not only once the (15-minute-default) access token happens to expire.

`require_authenticated_user` does more than verify the JWT: it also loads the referenced session by `sid` and confirms it hasn't been revoked, and loads the user by `sub` and confirms `is_active`. Both are live reads. This means **logout is effective immediately**, not just once the access token naturally expires — a deliberate choice given evidentiary-integrity stakes, at the cost of a request-time database read per authenticated call (see ADR-003).

Defaults: `AUTH_ACCESS_TOKEN_TTL_SECONDS=900` (15 minutes), `AUTH_REFRESH_TOKEN_TTL_SECONDS=1209600` (14 days). Both configurable; `AUTH_JWT_SECRET` must be ≥ 32 characters (enforced by a `Settings` validator) and has no default — a misconfigured deployment fails at startup, matching every other required setting in `app/core/config.py`.

## Refresh rotation and reuse detection

Every successful `/refresh` call:

1. Looks up the session by the SHA-256 hash of the presented refresh token.
2. Rejects (generic `401`) if not found, expired, or already revoked.
3. Otherwise **inserts a new session row in the same `token_family_id`, then revokes the old row and points its `replaced_by_session_id` at the new one** — insert-before-update, because `replaced_by_session_id` has an immediately-checked foreign key onto `auth_sessions.session_id` (see ADR-003).
4. Returns a new access token + a new opaque refresh token.

**Reuse detection**: if a refresh token that was already rotated away (`replaced_by_session_id IS NOT NULL`) is presented again, the *entire token family* is revoked — including whatever session it was rotated into, even if that session is still legitimately active. Reuse anywhere in a chain is treated as a signal the family may be compromised; the safe response is to force full re-authentication rather than trust the current leaf. A session revoked for an unrelated reason (logout) has `replaced_by_session_id IS NULL` and is a plain denial, not reuse detection.

## Multi-factor authentication (TOTP) and forced credential ceremony

See ADR-033 for why this exists and the frontend requirement that drove it.
`POST /api/v1/admin/users` now always creates an account with
`must_change_password=True` — the admin-chosen password is treated as a
one-time credential the investigator must replace before anything else, not
because the schema generates it (it doesn't; the admin still picks the
initial value), but because the server refuses to clear the flag until
`change_password` succeeds. `PublicUser`/`MeResponse` expose
`must_change_password` and `totp_enabled` so a frontend can gate its own
onboarding screens; neither the API nor this module enforces that ordering
on any *other* endpoint — it is a UI-level ceremony, not a server-side hard
lock on the rest of the API surface.

TOTP itself is a hand-rolled RFC 6238 implementation (`totp.py`, stdlib
`hmac`/`hashlib` only — SHA1, 6 digits, 30-second step, ±1 step drift
tolerance), not a third-party dependency; `password.py`'s own
already-narrow, reviewed dependency list was the reason not to add one.

```text
1. POST /auth/mfa/enroll        (authenticated) -> fresh secret, not yet enabled
2. POST /auth/mfa/verify        (authenticated) -> confirm one real code -> totp_enabled=True
3. POST /auth/login             -> mfa_required=true + mfa_token, once totp_enabled
4. POST /auth/mfa/login-verify  -> mfa_token + code -> real session (TokenPairResponse)
```

`TokenPairResponse` carries both shapes additively (`mfa_required`/`mfa_token`
alongside the original `access_token`/`refresh_token`/`expires_in`, all now
optional) — every pre-MFA caller of `/login` is unaffected as long as the
account has no TOTP enrolled. `enroll_mfa` can be called again before
`verify` confirms it (each call overwrites the pending secret); nothing
about MFA is ever silently auto-enabled — `totp_enabled` flips to `True`
only inside `confirm_mfa_enrollment`, after a real code has been checked.
`mfa/login-verify` is rate-limited independently (`AUTH_MFA_RATE_LIMIT`,
default 8/60s) — a 6-digit code has only 1e6 possibilities, so this stays as
tight as login itself.

**Admin-only lost-device/lost-password recovery**:
`POST /api/v1/admin/users/{user_id}/reset-credentials` (admin-only, never
self-service) reissues a fresh one-time temporary password, clears
`totp_secret`/`totp_enabled` entirely (the investigator re-enrolls from
scratch), sets `must_change_password=True` again, and immediately revokes
every live session for that account — a still-valid old access/refresh
token pair cannot outlive the reset.

**Admin-only account listing**: `GET /api/v1/admin/users` (`limit`/`offset`,
bounded 1-200, default 200) backs the Settings/Security admin sub-section's
account picker — the frontend needs a real way to choose *which* account to
reset without inventing one. Returns `PublicUser` rows only (same safe shape
as everywhere else; never a password hash or TOTP secret), oldest-first, no
audit event recorded (a plain read, exactly like `GET /admin/workers`).

## Role/action matrix

```text
                case_read  case_manage  evidence_read  evidence_write  graph_read  review_decide  hypothesis_propose  export_case_data
case_owner         X            X            X              X             X             X               X                  X
case_manager       X            X            X              X             X             X               X                  X
investigator       X                         X              X             X                              X
analyst            X                         X                            X
reviewer           X                         X                            X             X
viewer             X                                                      X
```

Encoded as `ROLE_ACTIONS: dict[CaseRole, frozenset[CaseAction]]` in `models.py`. An action not listed for a role is denied, full stop — there is no implicit fallback or wildcard. `hypothesis_propose` is a Phase 6 Part 5 addition (Shreshtha) — see `docs/architecture/phase-6-review-and-hypothesis.md`. This table predates, and does not yet include, Phase 6 Part 2's `integrity_read`/`integrity_verify`/`integrity_export` columns (Aditya) — see `app/modules/access_control/models.py::ROLE_ACTIONS` for the authoritative, current full matrix.

## Clearance and classification

```text
restricted  <  confidential  <  secret
```

A request is allowed only when **all** of the following hold (`policy.authorize_case_action`, always given an explicit `case_id` — never a globally-selected "current case"):

1. `case_id` was actually supplied.
2. The authenticated user is active.
3. The referenced case exists, and matches `case_id` exactly.
4. An active membership exists for that exact `(case_id, user_id)` pair.
5. The membership's role permits the requested action.
6. The membership's clearance ranks at or above the case's classification.
7. (Optional hook) the membership's clearance ranks at or above a supplied per-resource classification, for a later phase's per-evidence classification.

Any missing, inactive, mismatched, or unrecognized input denies — this is default-deny, not fail-open on an edge case. See `docs/architecture/security-boundaries-v1.md` for why every denial reason renders as the identical generic `403`.

## Rate limiting

`AUTH_LOGIN_RATE_LIMIT` (default 5) and `AUTH_REFRESH_RATE_LIMIT` (default 20) attempts per a fixed 60-second window, keyed by a SHA-256 hash of `(purpose, identifier)` — never the raw email/token. Backed by Redis in production (`RedisRateLimiter`, `INCR` + `EXPIRE`) with a deterministic in-memory implementation (`InMemoryRateLimiter`) for unit tests. **Fail-closed**: if Redis is unreachable, the limiter denies (same generic `429` as a normal over-limit hit) rather than silently allowing unlimited attempts — see ADR-003.

## Audit events

`security_audit_events` records safe security telemetry only: `event_type` (e.g. `auth.login.success`, `auth.refresh.reuse_detected`), `outcome`, `request_id`, nullable `user_id`/`case_id`, an IP hash (`audit.hash_ip`, never a raw address), and a `metadata_safe_json` blob callers must keep free of secrets. It **never** stores a password, access token, refresh token, password hash, raw evidence/CDR/financial value, full IP address, or stack trace. This is security telemetry for this module's own actions — not a substitute for Nipun's later tamper-evident evidence audit chain.

## Retry foundation

`retry.retry_async` provides bounded exponential backoff with jitter for explicitly idempotent, transient-failure-prone operations — a foundation for later service adapters. It refuses (without ever invoking the operation) any call named in `NEVER_RETRY_OPERATIONS`: `login`, `register`, `password_change`, `token_rotation`, `session_revocation`, `review_decision`, `evidence_write`. This module's own auth flows never call `retry_async` on themselves — none of register/login/refresh/logout/token-rotation is retried anywhere in this codebase.

## Integration points for later work

```python
from app.modules.access_control.dependencies import (
    require_authenticated_user,
    require_case_read,
    require_evidence_read,
    require_graph_read,
    require_review_decision,
    require_case_action,  # factory: require_case_action(CaseAction.EXPORT_CASE_DATA)
)
```

Each `require_case_*` dependency expects a `case_id: UUID` path parameter on the endpoint it's attached to, and returns an `AuthorizedCasePrincipal` (the authenticated principal, `case_id`, and the live `CaseMembershipRecord` used to authorize it) for the endpoint to use. These are the integration points for Nipun's future case/evidence endpoints and Shreshtha's future graph endpoints — none of which exist in this phase.
