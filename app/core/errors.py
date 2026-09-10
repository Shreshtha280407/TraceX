"""Safe, structured API error envelope, correlation ID plumbing, and exception handling.

No handler in this module may leak stack traces, connection strings, or
secrets to a client. Internal detail belongs in logs, keyed by the request's
correlation ID, not in the HTTP response body.

## Why the three exception handlers each set their own safe headers

It would be simpler if a single response-header middleware could add
`X-Request-ID` and the baseline security headers to *every* response and
be done with it. That does not work for one specific, important case, and
understanding why matters for anyone touching this file:

Starlette's `Starlette.build_middleware_stack()` treats a handler
registered for the literal `Exception` (or `500`) key specially: it is
passed as `ServerErrorMiddleware`'s own `handler` argument, not added to
`ExceptionMiddleware`'s handler map like every other registered exception
type (`HTTPException`, `RequestValidationError`, ...). `ServerErrorMiddleware`
is the outermost layer of the whole stack -- it wraps every user-added
`add_middleware(...)` call, including `RequestIDMiddleware` below and
`app.modules.access_control.api.SecurityHeadersMiddleware`. When it invokes
its own handler (`unhandled_exception_handler`) for a genuinely unhandled
exception, it sends the resulting response via the *raw* ASGI `send` it was
originally given -- which sits *outside* every user middleware, so none of
them ever see that response's `http.response.start` message to add headers
to it.

In other words: `unhandled_exception_handler`'s response is the one path
in this whole application that no middleware -- pure ASGI or otherwise --
can safely assume it will get a chance to touch. This was verified by
reading Starlette's own `build_middleware_stack` source and confirmed with
a live regression test (`tests/unit/test_error_handling.py`); see
`docs/architecture/security-boundaries-v1.md` for the full investigation.
The fix is for every handler in this file to build a fully-header-complete
response itself, via `_safe_error_headers`, rather than relying on
middleware to finish the job -- correct for all three handlers, but only
strictly *necessary* for `unhandled_exception_handler`.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the correlation ID for the currently handled request, if any.

    Reads a `contextvars.ContextVar` -- safe to call from arbitrary
    application code (e.g. audit logging) that has no direct handle on the
    current `Request`. Exception handlers in this file prefer
    `_resolve_request_id(request)` instead; see its docstring for why.
    """
    return _request_id_ctx.get()


def _resolve_request_id(request: Request) -> str:
    """The request-correlation ID, robust to exception-unwinding timing.

    `RequestIDMiddleware` resets its contextvar in a `finally` block --
    correct, and required so the value never leaks into an unrelated
    request -- but that `finally` runs *while an exception is still
    propagating* through the middleware's own stack frame. By the time
    Starlette's outermost `ServerErrorMiddleware` goes on to invoke
    `unhandled_exception_handler` for a truly unhandled exception, that
    reset has already happened and the contextvar reads back `""`.

    The ID stashed directly on `request.state` (set by `RequestIDMiddleware`
    on the same mutable ASGI `scope` dict that every `Request` in this
    call chain wraps by reference) is unaffected by that unwinding, so it
    is checked first; the contextvar remains the fallback for the rare
    case something reads it before `RequestIDMiddleware` ran at all.
    """
    stashed = getattr(request.state, "request_id", None)
    if isinstance(stashed, str) and stashed:
        return stashed
    return get_request_id()


class ErrorCode:
    """Stable, documented error codes returned in API error envelopes."""

    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"


#: Maps a `HTTPException.status_code` to the specific `ErrorCode` its body
#: should carry. Anything not listed falls back to `VALIDATION_ERROR`,
#: preserving the original behavior for status codes this map doesn't know
#: about.
_STATUS_CODE_TO_ERROR_CODE: dict[int, str] = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    status.HTTP_500_INTERNAL_SERVER_ERROR: ErrorCode.INTERNAL_ERROR,
}

#: Safe, static security headers every response this module builds
#: directly carries. Kept in sync in spirit (not by import, to avoid a
#: reverse dependency from core onto a feature module) with
#: `app.modules.access_control.api.SecurityHeadersMiddleware`, which
#: applies the same three headers to every *normal* (non-error-handler)
#: response.
_BASELINE_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def error_body(code: str, message: str, request_id: str) -> dict[str, Any]:
    """Build the canonical error envelope body."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _safe_error_headers(request_id: str) -> dict[str, str]:
    """Headers every error-envelope response carries, regardless of which
    Starlette layer ends up building it.

    `Cache-Control: no-store` is included unconditionally -- an error
    response body should never be cached by a client or proxy, regardless
    of which endpoint produced it (a stricter, simpler rule than "only on
    `/api/v1/auth/*`", and one this module can apply without needing any
    path-specific knowledge of a feature module's routes).
    """
    return {
        REQUEST_ID_HEADER: request_id,
        "Cache-Control": "no-store",
        **_BASELINE_SECURITY_HEADERS,
    }


class RequestIDMiddleware:
    """Pure ASGI middleware: attaches a request-correlation ID to context and response headers.

    Implemented as raw ASGI, not `starlette.middleware.base.BaseHTTPMiddleware`:
    this only needs to read one incoming header and mutate one outgoing
    header, which a `send` wrapper does directly and safely with no need
    for `BaseHTTPMiddleware.dispatch`'s heavier Request/Response
    reconstruction. (That reconstruction was investigated as a suspected
    cause of a separate, previously-reported exception-handling gap; it
    turned out not to be the actual cause -- see the module docstring and
    `docs/architecture/security-boundaries-v1.md` for the real one. This
    class is still implemented as pure ASGI because it is the simpler,
    more direct tool for exactly this job, not because `BaseHTTPMiddleware`
    was proven unsafe.)

    Passes non-HTTP scopes (`lifespan`, `websocket`) straight through
    untouched -- there is no HTTP request/response here to correlate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming else str(uuid.uuid4())
        # Survives exception unwinding even after the contextvar below is
        # reset -- see `_resolve_request_id`'s docstring.
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id_ctx.set(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _request_id_ctx.reset(token)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render `HTTPException`s using the safe error envelope.

    Preserves any headers the raised `HTTPException` carried (e.g. a
    `WWW-Authenticate` challenge on a 401) alongside the safe baseline --
    `setdefault` so a caller-supplied header can never silently override
    `Cache-Control`/the security headers below.
    """
    assert isinstance(exc, StarletteHTTPException)
    request_id = _resolve_request_id(request)
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    code = _STATUS_CODE_TO_ERROR_CODE.get(exc.status_code, ErrorCode.VALIDATION_ERROR)
    headers = _safe_error_headers(request_id)
    if exc.headers:
        for key, value in exc.headers.items():
            headers.setdefault(key, value)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, detail, request_id),
        headers=headers,
    )


async def request_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render request-body validation errors without ever echoing submitted values.

    FastAPI's default handler includes the raw submitted value (`"input"`)
    for every error, at the *model* level -- so a request body carrying a
    password or token alongside one other invalid field (e.g. a missing
    `email`) would have that secret echoed straight back in the response,
    since the "input" for a missing-field error is the whole partial body.
    This handler keeps only the error location/type/message, never a value.
    """
    assert isinstance(exc, RequestValidationError)
    request_id = _resolve_request_id(request)
    errors = [
        {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
        for error in exc.errors()
    ]
    body = error_body(ErrorCode.VALIDATION_ERROR, "Request validation failed", request_id)
    body["errors"] = errors
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=body,
        headers=_safe_error_headers(request_id),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler: logs the exception type server-side, never leaks internals to the client.

    Never logs `str(exc)` -- only its type -- since some drivers embed a
    connection string or credential directly in the exception message
    (the same rule already applied to `/readyz` in
    `app/dependencies/services.py`). See the module docstring for why this
    handler in particular must set every response header itself rather
    than relying on middleware.
    """
    request_id = _resolve_request_id(request)
    logger.warning(
        "unhandled_exception",
        request_id=request_id,
        path=request.url.path,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(ErrorCode.INTERNAL_ERROR, "An internal error occurred", request_id),
        headers=_safe_error_headers(request_id),
    )
