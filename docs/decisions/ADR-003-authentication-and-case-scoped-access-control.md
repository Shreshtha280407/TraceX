# ADR-003: Authentication and Case-Scoped Access Control

- Status: Accepted (Phase 1 access-control foundation)
- Owner: Aditya
- Date: 2026-09-10

## Context

`app/modules/access_control/` needs to add TraceX's first real authentication, session security, and case-scoped RBAC/ABAC on top of Nipun's frozen foundation, without touching `app/contracts/` and without colliding with Shreshtha's graph module or Jasraj's structured-processing module. Several decisions had no single obviously-correct answer; they're recorded here so later contributors know what was deliberate.

## Decision 1: `require_authenticated_user` re-checks the session and user live, every request

**Decision**: Beyond JWT signature/issuer/audience/expiry/type validation, `require_authenticated_user` also loads the session by `sid` (rejecting if revoked) and the user by `sub` (rejecting if inactive) — both live database reads, on every authenticated request.

**Why**: The task brief requires that case access "be evaluated against current membership data so revocation takes effect correctly," and separately requires logout to actually revoke the session. If access tokens were validated purely by signature/expiry, a logged-out session's access token would keep working for up to its full 15-minute TTL — a real, if narrow, evidentiary-integrity gap in a system built around auditability. The cost is one extra `SELECT` per authenticated request; given this system's stakes (criminal-investigation evidence access) over raw throughput, that trade was made deliberately. Case-level authorization (`require_case_action`) reads live data unconditionally regardless of this decision — this is specifically about *identity* revocation being immediate too, not just case membership revocation.

## Decision 2: every case-action denial renders as the identical generic `403`

**Decision**: `policy.authorize_case_action` returns a plain `bool`; `dependencies.require_case_action` raises the same `HTTPException(403, "access denied")` whether the case doesn't exist, the membership doesn't exist, the membership is inactive, the role doesn't permit the action, or the clearance is insufficient.

**Why**: A case ID is not secret the way a password is, but *which* of five different reasons blocked access is itself information a probing caller could use to enumerate case existence or a target's clearance level — e.g. distinguishing "case not found" from "found, but you lack clearance" tells an attacker the case exists. Default-deny, uniformly, closes that channel. This mirrors the same anti-enumeration principle the task brief mandates for login, extended to case authorization.

## Decision 3: Redis rate-limiter fails closed on outage

**Decision**: If Redis is unreachable, `RedisRateLimiter.check_and_increment` returns `False` (deny) rather than `True` (allow).

**Why**: The task brief asks for "a documented safe fail-open/fail-closed MVP policy" and to "prefer a conservative documented policy and test it." Fail-open would mean a Redis outage — which an attacker could potentially induce — removes all brute-force protection on login exactly when it matters most. Fail-closed's downside (legitimate users briefly unable to log in during a real Redis outage) is a worse *availability* story but a much better *security* story for an evidentiary system; availability degradation is visible and recoverable, a silent brute-force window is not. Tested directly in `tests/unit/access_control/test_rate_limit.py::test_redis_limiter_fails_closed_on_outage`.

## Decision 4: a dummy Argon2id verification closes the login timing side-channel

**Decision**: `service.login` always performs a real `verify_password` call — against a fixed dummy hash (`_DUMMY_PASSWORD_HASH`, computed once at import time) when the email is unknown or the account is inactive, and against the real stored hash otherwise.

**Why**: The task brief requires login failure to "not reveal whether a particular email exists." An identical response *body* isn't sufficient on its own: skipping Argon2id entirely for an unknown email would make that request measurably faster than a wrong-password attempt against a real account, and Argon2id is deliberately slow (tens of milliseconds) — an easily measurable timing side channel over even a handful of requests. Running the same expensive operation regardless of outcome removes the timing signal, not just the body signal.

## Decision 5: registration conflicts are *not* anti-enumeration-protected; login is

**Decision**: `POST /api/v1/auth/register` returns `409 Conflict` with a message revealing the email is already registered. `POST /api/v1/auth/login` never reveals whether an email is registered.

**Why**: These are different threat models. Login anti-enumeration protects a stranger from learning who has an account by trying to log in as them. Registration is the caller actively asserting ownership of an email address they're trying to claim — virtually every mainstream registration flow (with or without email verification) confirms "this email is taken" at this step, and treating it as secret provides negligible security benefit while meaningfully hurting UX (a user who mistypes an old registration would get a confusing "check your email" for an account they don't control). This is a deliberate, scoped exception to the general "never reveal account existence" instinct, not an oversight.

## Decision 6: hand-written Alembic migration; SQLAlchemy Core, not a project-wide ORM

**Decision**: `migrations/versions/7e8499f34f29_access_control_foundation.py` is hand-written (`op.create_table(...)` calls), not autogenerated. `repository.py` defines its own SQLAlchemy Core `Table` objects for the five tables, duplicating the column list from the migration by hand rather than sharing one declarative model.

**Why**: `migrations/env.py` keeps `target_metadata = None` — Nipun's own baseline decision, documented in `docs/architecture/phase-1-decisions.md` as "later phases point this at their SQLAlchemy declarative metadata." Wiring that up now would mean introducing a project-wide declarative `Base` as a new shared convention on Nipun's behalf, a bigger and less clearly-scoped change than this phase calls for. Hand-writing both the migration and the Core `Table` objects keeps every change inside this module's own files (plus one new migration file), fully additive, at the cost of needing to keep the two in sync by hand if columns ever change — documented at the top of both files.

## Decision 7: `rotate_session` inserts the new session before revoking the old one

**Decision**: `repository.rotate_session` runs `INSERT` (new session) then `UPDATE` (old session, setting `revoked_at` and `replaced_by_session_id`) — in that order, inside one transaction.

**Why**: `auth_sessions.replaced_by_session_id` has a foreign key onto `auth_sessions.session_id`, checked immediately by PostgreSQL (this schema declares no `DEFERRABLE` constraints). The original implementation ran `UPDATE` first, pointing the old row at a session ID that didn't exist yet — which passed every unit test (the in-memory `FakeAccessControlRepository` doesn't enforce foreign keys) but failed with `ForeignKeyViolationError` the moment it ran against real PostgreSQL in `tests/integration/access_control/test_auth_lifecycle_live.py`. This is exactly the kind of bug the task's requirement for live integration tests exists to catch — see `docs/qa/test-results.md` for the concrete failure and fix.

## Decision 8: extending `app/core/errors.py`'s `ErrorCode`/`http_exception_handler`

**Decision**: Added `ErrorCode.UNAUTHORIZED`/`FORBIDDEN`/`CONFLICT`/`RATE_LIMITED` and a status-code-to-error-code map in `http_exception_handler`, and made it preserve `HTTPException.headers` (e.g. `WWW-Authenticate`). `app/core/errors.py` is on the phase brief's explicit list of shared files this work may edit.

**Why**: Before this change, *every* non-404 `HTTPException` — a `401` from a bad token, a `403` policy denial, a `429` rate limit, this module's own `500` — rendered with the same `"code": "validation_error"`, which is misleading (an internal server error is not a validation error) and would only get more confusing as more of this module's endpoints came online. The fix generalizes cleanly to any future endpoint raising the same status codes, not just this module's; it's additive (no existing code changed meaning) and required almost no code (a status-code lookup table). Silently working around it locally instead (e.g. wrapping every `HTTPException` in a differently-shaped custom response) would have meant *not* reusing Nipun's existing safe-error-envelope pattern, which seemed like the worse outcome.

## Decision 9: no cookie authentication or CSRF this phase

**Decision**: Authorization-header access tokens and JSON-body refresh tokens only; no cookie is ever set.

**Why**: Explicit in the phase brief — CSRF hardening is meaningless without a cookie to protect, and adding cookie support (plus the CSRF defenses it would require) with no browser frontend to consume it would be speculative scaffolding. Flagged for team review the moment a browser frontend adopts cookie-based storage.

## Decision 10: security headers and `Cache-Control: no-store` applied at the middleware layer (later revised)

**Decision**: `SecurityHeadersMiddleware` sets headers on every response by wrapping the whole request/response cycle, rather than each endpoint mutating an injected `Response` object.

**Why**: An injected `Response` object's header mutations are discarded the instant the endpoint raises an exception instead of returning normally — the client receives whatever the registered exception handler built instead. A rate-limited (`429`) or credential-rejected (`401`) response is exactly the case where these headers matter most, so per-endpoint mutation would silently miss them on the paths that need them. Middleware sees the final response regardless of how it was produced.

**Revised finding (Integration Hardening 1)**: the reasoning above turned out to be *incomplete*, not wrong — middleware sees the final response for every path *except one*. Starlette's outermost `ServerErrorMiddleware` dispatches a handler registered for the bare `Exception` key directly, bypassing every user-added middleware (including this one) for that specific response. This module's own per-endpoint/per-dependency `_internal_error` workaround (originally attributed, incorrectly, to a `BaseHTTPMiddleware`-specific bug — see below) happened to sidestep this gap as a side effect, without anyone having identified it as the real issue. The actual fix: `app/core/errors.py`'s exception handlers now set their own headers directly via a shared `_safe_error_headers()` helper, and the per-endpoint workaround was removed as redundant. Full investigation and fix in `docs/architecture/security-boundaries-v1.md`'s "Integration Hardening 1" section — including the correction that `BaseHTTPMiddleware` was never actually the cause of the originally-reported symptom (verified against Starlette's own source and a live `uvicorn` process). `RequestIDMiddleware` and `SecurityHeadersMiddleware` were still converted to pure ASGI middleware as part of this work, but as an independent, well-justified simplification -- not as the fix for the reported bug.

## Open questions for team review

- Whether case-level authorization should also support a per-evidence classification check once evidence records exist (the `resource_classification` hook in `policy.authorize_case_action` is unused today — see `docs/architecture/access-control-v1.md`).
- Whether `require_authenticated_user`'s per-request live session/user check is an acceptable latency cost once real case/evidence endpoints exist under load — untested at scale in this phase.
- CORS and cookie/CSRF policy, deferred until a browser frontend exists (see `docs/architecture/security-boundaries-v1.md`).
