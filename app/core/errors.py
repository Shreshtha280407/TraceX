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
    """Render `HTTPException`s using the safe error envelope."""
    assert isinstance(exc, StarletteHTTPException)
    request_id = get_request_id()
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(ErrorCode.NOT_FOUND, detail, request_id)
        if exc.status_code == status.HTTP_404_NOT_FOUND
        else error_body(ErrorCode.VALIDATION_ERROR, detail, request_id),
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
