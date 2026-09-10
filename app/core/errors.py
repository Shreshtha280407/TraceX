"""Safe, structured API error envelope and correlation ID plumbing.

No handler in this module may leak stack traces, connection strings, or
secrets to a client. Internal detail belongs in logs, keyed by the request's
correlation ID, not in the HTTP response body.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the correlation ID for the currently handled request, if any."""
    return _request_id_ctx.get()


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
#: should carry. Added for `app/modules/access_control/`, which raises
#: 401/403/409/429 -- extended here (not duplicated per-module) since any
#: future endpoint raising the same status codes benefits identically.
#: Anything not listed falls back to `VALIDATION_ERROR`, preserving the
#: original behavior for status codes this map doesn't know about.
_STATUS_CODE_TO_ERROR_CODE: dict[int, str] = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    status.HTTP_500_INTERNAL_SERVER_ERROR: ErrorCode.INTERNAL_ERROR,
}


def error_body(code: str, message: str, request_id: str) -> dict[str, Any]:
    """Build the canonical error envelope body."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a correlation ID to every request and echo it on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Any:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming else str(uuid.uuid4())
        token = _request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render `HTTPException`s using the safe error envelope.

    Preserves any headers the raised `HTTPException` carried (e.g. a
    `WWW-Authenticate` challenge on a 401) -- these would otherwise be
    silently dropped, since the response returned here, not the raised
    exception, is what actually reaches the client.
    """
    assert isinstance(exc, StarletteHTTPException)
    request_id = get_request_id()
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    code = _STATUS_CODE_TO_ERROR_CODE.get(exc.status_code, ErrorCode.VALIDATION_ERROR)
    headers = dict(exc.headers) if exc.headers else {}
    headers[REQUEST_ID_HEADER] = request_id
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
    request_id = get_request_id()
    errors = [
        {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
        for error in exc.errors()
    ]
    body = error_body(ErrorCode.VALIDATION_ERROR, "Request validation failed", request_id)
    body["errors"] = errors
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=body,
        headers={REQUEST_ID_HEADER: request_id},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler: never leak exception internals to the client."""
    request_id = get_request_id()
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(ErrorCode.INTERNAL_ERROR, "An internal error occurred", request_id),
        headers={REQUEST_ID_HEADER: request_id},
    )
