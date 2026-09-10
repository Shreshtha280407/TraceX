# Security Boundaries v1

Owner: Aditya. What this phase's security middleware, error handling, and transport-level protections do and do not cover. See `docs/architecture/access-control-v1.md` for the authentication/authorization design itself, and `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md` for the reasoning.

## Security headers

`app.modules.access_control.api.SecurityHeadersMiddleware` (registered in `app/main.py`, alongside Nipun's existing `RequestIDMiddleware`) adds to every response:

```text
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
```

and, for every path under `/api/v1/auth/*` specifically:

```text
Cache-Control: no-store
```

These are applied at the **middleware** layer, not per-endpoint, deliberately: a header set on the `Response` object injected into a route handler is discarded the moment that handler raises an exception instead of returning normally, since the client actually receives whatever a registered exception handler builds instead. Middleware wraps the whole request/response cycle including exception-handler output, so this is the only way to guarantee these headers (especially `Cache-Control: no-store` on a rate-limited or credential-rejected response) appear on *every* outcome, not just the happy path.

## CORS: intentionally not configured

No CORS middleware is added in this phase, and no `Access-Control-Allow-Origin` header is ever set — not even a permissive `*`. Phase 1 has no browser frontend consumer, so there is nothing to grant cross-origin access to; adding CORS now would be unrequested, speculative scaffolding. **When a browser frontend is introduced, it must configure explicit development origins (never `*`) and this decision must be revisited** — flagged for team review in `docs/qa/known-limitations.md`.

## No cookie authentication or CSRF in this phase

Access tokens travel in the `Authorization: Bearer <token>` header; refresh tokens travel in an explicit JSON request body (`POST /api/v1/auth/refresh`/`/logout`). Neither is ever set as a cookie. This sidesteps CSRF entirely for this phase's transport model — CSRF is a cookie-specific attack (a browser auto-attaching a cookie to a cross-site request), and there is no cookie to auto-attach. **If a future browser frontend adopts cookie-based token storage, CSRF hardening (double-submit token, `SameSite` cookies, or a synchronizer token) becomes a hard requirement at that point** — explicitly deferred, not solved here.

## Never expose API stack traces

Nipun's existing `app/core/errors.py` already keeps `unhandled_exception_handler` from ever echoing exception text. This phase extends that file additively:

- **`request_validation_exception_handler`** (new): FastAPI's *default* handler for a pydantic/`RequestValidationError` includes the raw submitted value (`"input"`) per error, at the whole-model level — so a request body carrying a password or token alongside one other invalid field (e.g. a missing `email`) would echo that secret straight back, since the `"input"` for a *missing*-field error is the entire partial body dict. The new handler keeps only `loc`/`type`/`msg`, never a value.
- **`ErrorCode`** gained `UNAUTHORIZED`/`FORBIDDEN`/`CONFLICT`/`RATE_LIMITED` (additive; existing codes unchanged), and `http_exception_handler` now maps `401`/`403`/`404`/`409`/`429`/`500` to a specific code instead of collapsing everything but `404` into `validation_error` — and now also preserves any headers the raised `HTTPException` carried (e.g. `WWW-Authenticate: Bearer` on a `401`), which the original handler silently dropped.
- **A real Starlette/`BaseHTTPMiddleware` interaction, worked around locally.** On the Starlette version pinned here, an exception that propagates all the way out of a route handler can escape the app's registered generic-`Exception` handler entirely (reproduced with *only* Nipun's pre-existing `RequestIDMiddleware` in the stack — independent of this module's own middleware). Every `access_control` endpoint and authorization dependency therefore catches unexpected exceptions itself (`api._internal_error` / `dependencies._internal_error`) and raises a plain `HTTPException(500, "an internal error occurred")` — which *is* handled correctly by the framework's normal exception-handling path — logging only the exception's *type* via `structlog`, never its message (some drivers embed connection strings in exception text) or a stack trace. This is a local, scoped workaround inside this module; it does not change Nipun's shared middleware/exception-handling code beyond the two additive items above.

## Rate-limit outage policy: fail-closed

If Redis is unreachable when a login/refresh attempt needs a rate-limit check, `RedisRateLimiter.check_and_increment` returns `False` — the exact same outcome as a normal over-limit hit, rendering the same generic `429`. This is the conservative choice: the alternative (fail-open) would let an attacker force a Redis outage specifically to bypass login rate limiting. See `tests/unit/access_control/test_rate_limit.py::test_redis_limiter_fails_closed_on_outage`.

## What is not covered by this phase

- **No MFA, no SSO, no external identity provider.** Single-factor email+password only.
- **No production secret manager.** `AUTH_JWT_SECRET`/`POSTGRES_PASSWORD`/etc. come from `.env` (git-ignored) for local development only.
- **No case CRUD API.** `cases`/`case_memberships` are a minimal access-control anchor with repository methods only — no `/api/v1/cases` endpoints exist to create/list/update them in this phase (that's Nipun's later work, against `require_case_*` dependencies this module provides).
- **No per-evidence classification.** `policy.authorize_case_action`'s `resource_classification` parameter is a forward-compatible hook for it, unused by anything in this phase.
- See `docs/qa/known-limitations.md` for the complete list.

## LAN boundary

See `docs/runbooks/lan-development.md` for the full local-network setup. Summary: PostgreSQL, Neo4j, Redis, and MinIO are never exposed to the LAN — only the API port is, from one designated host, over plain HTTP, for local demo/development only.
