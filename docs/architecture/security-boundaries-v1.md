# Security Boundaries v1

Owner: Aditya. What this phase's security middleware, error handling, and transport-level protections do and do not cover. See `docs/architecture/access-control-v1.md` for the authentication/authorization design itself, and `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md` for the reasoning.

## Integration Hardening 1: central middleware and exception handling

**Status: In progress.** This section supersedes the original "worked around locally" text below (kept, struck through in spirit, for history) — this workstream replaced the per-endpoint/per-dependency `_internal_error` workaround in `app/modules/access_control/` with one central design, after re-investigating the root cause from scratch rather than trusting the original diagnosis.

### What was originally believed, and what turned out to be true

The original diagnosis (recorded in ADR-003, Decision 10, and `docs/qa/known-limitations.md`) was: *"an exception that propagates out of a route handler can escape the app's registered generic-`Exception` handler when `BaseHTTPMiddleware`-based middleware is in the stack."* Re-investigating this from scratch (reading Starlette's own source, not just re-running the symptom) found that diagnosis **incorrect**:

1. The observed symptom — `await client.get(...)` itself raising the original exception instead of returning a `Response` — reproduces identically with **zero custom middleware** in the stack. `BaseHTTPMiddleware` was never the cause.
2. The actual cause: Starlette's `ServerErrorMiddleware` (present in **every** Starlette/FastAPI app unconditionally, regardless of any user middleware) intentionally re-raises a truly unhandled exception *after* already sending the safe response — its own source says so directly: *"We always continue to raise the exception. This allows servers to log the error, or allows test clients to optionally raise the error within the test case."* httpx's `ASGITransport`, used by this project's `client` test fixture, defaults to `raise_app_exceptions=True`, which propagates that re-raise to the *test caller* instead of returning the response that was already correctly built and sent.
3. Verified against a **real** `uvicorn` process (not a test transport) with `BaseHTTPMiddleware`-based middleware in the stack: a route raising an unhandled `OSError` containing a fake secret returned the exact safe `{"error": {"code": "internal_error", ...}}` body over real HTTP, `200`/`500` status correct, secret absent from the response — while the full traceback (secret included) was logged **server-side only**, exactly as intended. Production traffic was never at risk.
4. A **second, genuinely real** issue was found during the same investigation (not previously known, not part of the original diagnosis): Starlette's `build_middleware_stack()` routes a handler registered for the bare `Exception` (or `500`) key directly to `ServerErrorMiddleware` as its own `handler` — not into `ExceptionMiddleware`'s handler map like every other registered type (`HTTPException`, `RequestValidationError`). Since `ServerErrorMiddleware` is the absolute outermost layer, wrapping every user-added middleware, its handler's response is sent via the *raw* ASGI `send` it originally received — bypassing every user middleware's header injection entirely, for that one response path only. This means `unhandled_exception_handler`'s response previously had **no correlation ID and no security headers**, regardless of what middleware existed or how it was implemented (`BaseHTTPMiddleware` or pure ASGI made no difference to this specific gap).

### The central fix

1. **`app/core/errors.py`'s three exception handlers now build fully header-complete responses themselves**, via a shared `_safe_error_headers()` helper (`X-Request-ID`, `Cache-Control: no-store`, and the baseline security headers), rather than relying on middleware to add them afterward. This is what actually closes item 4 above — it is necessary specifically because `unhandled_exception_handler` can never assume any middleware saw its response, and applied to all three handlers for consistency and defense in depth.
2. **`RequestIDMiddleware` (`app/core/errors.py`) and `SecurityHeadersMiddleware` (`app/modules/access_control/api.py`) were rewritten as pure ASGI middleware**, not because `BaseHTTPMiddleware` caused the investigated symptom (it didn't — see above), but because both middlewares only ever needed to read one header and mutate response headers, which a `send`-wrapping pure ASGI class does directly, with no dependency on `BaseHTTPMiddleware.dispatch`'s heavier Request/Response reconstruction and its own separately-documented edge cases (e.g. background-task/cancellation interactions on client disconnect) that this change sidesteps as a bonus, not as the fix for the reported bug.
3. **`tests/conftest.py`'s shared `client` fixture (and every access-control test file's own override of it) now sets `ASGITransport(..., raise_app_exceptions=False)`**, so tests observe exactly what a real client/server exchange produces, instead of a test-transport-only re-raise being mistaken for a production safety gap. This is the fix for item 1-3 above and is what makes it possible to write a clean regression test for "an unexpected exception produces a safe 500" without a local workaround.
4. **The `_internal_error` per-endpoint/per-dependency workaround in `app/modules/access_control/api.py` and `dependencies.py` was removed.** It is now provably redundant: `tests/security/access_control/test_auth_no_secret_leakage.py::test_unexpected_backend_failure_never_leaks_a_connection_secret` (unchanged assertions) continues to pass with the workaround gone, proving the central handler alone is sufficient. The module-local `structlog` warning it used to emit is now emitted centrally instead, from `unhandled_exception_handler` itself (`event="unhandled_exception"`, exception *type* and `request.url.path` only, matching the same "never log the exception message" rule).

### Middleware ordering

`app/main.py` still adds `RequestIDMiddleware` before `SecurityHeadersMiddleware`; since both now mutate disjoint header keys via `send`-wrapping, their relative order does not affect correctness (verified by `tests/unit/test_error_handling.py`). Starlette's own `ServerErrorMiddleware` is always outermost regardless of `add_middleware` order — this is fixed framework behavior, not something `app/main.py` controls, and is exactly why item 4 above needed a handler-level fix rather than a middleware-ordering fix.

### Correlation ID: how it survives exception unwinding

`RequestIDMiddleware` still resets its `contextvars.ContextVar` in a `finally` block (required so the value can never leak into an unrelated request — verified by `tests/unit/test_error_handling.py::test_request_id_context_does_not_leak_between_requests`). That reset happens *while an exception is still propagating* through the middleware's own stack frame, so by the time `ServerErrorMiddleware` invokes `unhandled_exception_handler`, the contextvar has already gone back to `""`. To make the ID available anyway, `RequestIDMiddleware` also stashes it on `scope.setdefault("state", {})["request_id"]` — the same mutable ASGI `scope` dict every `Request` in the call chain wraps by reference, unaffected by which frame is currently unwinding. All three exception handlers read the ID via `_resolve_request_id(request)`, which checks `request.state.request_id` first and falls back to the contextvar. Verified directly by `tests/unit/test_error_handling.py::test_request_id_present_on_the_deep_unhandled_exception_path`.

### Local access-control workarounds: removed, with evidence

`app/modules/access_control/api.py`'s and `dependencies.py`'s `_internal_error` helper and every `except Exception: raise _internal_error(exc) from exc` clause were deleted outright (not simplified/kept-as-fallback). Evidence this is safe: `tests/unit/access_control/` and `tests/security/access_control/` (139 tests) pass unchanged after removal, including the one test specifically designed to exercise this exact path (`test_unexpected_backend_failure_never_leaks_a_connection_secret`).

### Remaining framework limitation

Starlette's `ServerErrorMiddleware` re-raising after sending a response (see point 2 above) is fixed, documented, upstream behavior — not something this project can or should change. Any *new* test that deliberately exercises a genuinely-unhandled-exception path must use `ASGITransport(..., raise_app_exceptions=False)` (or catch the exception itself), or it will see the exception re-raised to the test instead of a `Response` object. This is now documented at each fixture definition site so it isn't rediscovered as a "bug" again.

## Security headers

`app.modules.access_control.api.SecurityHeadersMiddleware` (pure ASGI; registered in `app/main.py`, alongside Nipun's `RequestIDMiddleware`, also pure ASGI — see "Integration Hardening 1" above) adds to every normal response:

```text
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
```

and, for every path under `/api/v1/auth/*` specifically:

```text
Cache-Control: no-store
```

Error-envelope responses (from any of the three central exception handlers in `app/core/errors.py`) carry the same three baseline headers plus `Cache-Control: no-store` **unconditionally, for every path** — not only `/api/v1/auth/*` — since an error body should never be cached regardless of which endpoint produced it, and the shared `app/core/errors.py` module has no reason to know about a specific feature module's path prefix. See "Integration Hardening 1" above for exactly why these are set directly by the handlers rather than solely by middleware.

## CORS: intentionally not configured

No CORS middleware is added in this phase, and no `Access-Control-Allow-Origin` header is ever set — not even a permissive `*`. Phase 1 has no browser frontend consumer, so there is nothing to grant cross-origin access to; adding CORS now would be unrequested, speculative scaffolding. **When a browser frontend is introduced, it must configure explicit development origins (never `*`) and this decision must be revisited** — flagged for team review in `docs/qa/known-limitations.md`.

## No cookie authentication or CSRF in this phase

Access tokens travel in the `Authorization: Bearer <token>` header; refresh tokens travel in an explicit JSON request body (`POST /api/v1/auth/refresh`/`/logout`). Neither is ever set as a cookie. This sidesteps CSRF entirely for this phase's transport model — CSRF is a cookie-specific attack (a browser auto-attaching a cookie to a cross-site request), and there is no cookie to auto-attach. **If a future browser frontend adopts cookie-based token storage, CSRF hardening (double-submit token, `SameSite` cookies, or a synchronizer token) becomes a hard requirement at that point** — explicitly deferred, not solved here.

## Never expose API stack traces

Nipun's existing `app/core/errors.py` already keeps `unhandled_exception_handler` from ever echoing exception text. This phase, and the "Integration Hardening 1" workstream above, extend that file additively:

- **`request_validation_exception_handler`** (new): FastAPI's *default* handler for a pydantic/`RequestValidationError` includes the raw submitted value (`"input"`) per error, at the whole-model level — so a request body carrying a password or token alongside one other invalid field (e.g. a missing `email`) would echo that secret straight back, since the `"input"` for a *missing*-field error is the entire partial body dict. The new handler keeps only `loc`/`type`/`msg`, never a value.
- **`ErrorCode`** gained `UNAUTHORIZED`/`FORBIDDEN`/`CONFLICT`/`RATE_LIMITED` (additive; existing codes unchanged), and `http_exception_handler` now maps `401`/`403`/`404`/`409`/`429`/`500` to a specific code instead of collapsing everything but `404` into `validation_error` — and now also preserves any headers the raised `HTTPException` carried (e.g. `WWW-Authenticate: Bearer` on a `401`), which the original handler silently dropped.
- **`unhandled_exception_handler` now also logs the exception server-side** (`structlog`, exception *type* and request path only — never the message, which for some drivers embeds a connection string or credential) before building its safe response, closing a real observability gap: previously, once the module-local `_internal_error` workaround below is removed, an unexpected exception would have produced a safe response with *no* server-side record of it having happened at all.
- ~~A real Starlette/`BaseHTTPMiddleware` interaction, worked around locally~~ — **superseded.** This diagnosis was investigated further and found incorrect; see "Integration Hardening 1" above for the actual root cause (a Starlette architectural detail unrelated to `BaseHTTPMiddleware`) and the central fix that replaced the per-endpoint/per-dependency workaround this bullet used to describe.

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
