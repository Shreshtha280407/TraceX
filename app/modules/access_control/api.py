"""Auth HTTP layer: the `/api/v1/auth` router and the security-headers middleware.

Endpoints are thin: parse the request, call `AuthService`, translate typed
errors from `errors.py` into safe, generic HTTP responses. No business
logic lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.errors import get_request_id
from app.modules.access_control.audit import hash_ip
from app.modules.access_control.dependencies import get_auth_service, require_authenticated_user
from app.modules.access_control.errors import (
    AuthenticationError,
    RateLimitExceededError,
    RefreshReuseDetectedError,
    SessionRevokedError,
    ValidationError,
)
from app.modules.access_control.models import (
    AuthenticatedPrincipal,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    PublicUser,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
)
from app.modules.access_control.service import AuthService, RequestContext

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_RATE_LIMIT_DETAIL = "too many attempts, try again later"
_LOGIN_DENIED_DETAIL = "invalid email or password"
_REFRESH_DENIED_DETAIL = "invalid or expired refresh token"
_INTERNAL_ERROR_DETAIL = "an internal error occurred"

#: Paths that carry credentials/tokens and must never be cached by a
#: client, proxy, or browser history.
_NO_STORE_PATH_PREFIX = "/api/v1/auth"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Safe, static security headers on every response.

    Applied here, at the middleware layer, so it covers *every* response
    path -- including ones built by FastAPI's own exception handlers for a
    raised `HTTPException` -- not just the happy path a per-endpoint header
    mutation would miss (an injected `Response` object's headers are
    discarded once an exception is raised instead of returned).

    Deliberately does not implement CORS: Phase 1 has no browser frontend
    consumer, so no `Access-Control-Allow-Origin` is set at all (never
    `*`) -- see `docs/architecture/security-boundaries-v1.md`.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(_NO_STORE_PATH_PREFIX):
            response.headers["Cache-Control"] = "no-store"
        return response


def _internal_error(exc: Exception) -> HTTPException:
    """Convert an unexpected failure (not one of this module's typed errors) into a safe 500.

    This module's endpoints raise `HTTPException` deliberately for every
    known outcome (see each `except` clause below); this is the backstop
    for everything else -- a database/Redis outage, for example. Logs the
    exception *type* only, via `structlog` -- never its message, which for
    some drivers can itself contain a connection string or credential
    (same rule already applied to `/readyz` in `app/api/health.py`).

    Also works around a real Starlette/`BaseHTTPMiddleware` interaction on
    the version pinned here: an exception that propagates all the way out
    of a route handler can escape the app's registered generic-`Exception`
    handler entirely instead of becoming a clean 500 (reproduced with only
    Nipun's pre-existing `RequestIDMiddleware` in the stack, independent of
    this module's own middleware) -- catching it here, inside the route
    handler, means it is always a plain `HTTPException` by the time it
    would reach that middleware layer, which is unaffected by the issue.
    """
    logger.warning("access_control_unhandled_error", exc_type=type(exc).__name__)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_INTERNAL_ERROR_DETAIL
    )


def _build_context(request: Request) -> RequestContext:
    client_host = request.client.host if request.client else None
    return RequestContext(
        now=datetime.now(UTC),
        request_id=get_request_id(),
        ip_marker=hash_ip(client_host) if client_host else None,
    )


@router.post("/register", response_model=PublicUser, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> PublicUser:
    try:
        return await auth_service.register(body, _build_context(request))
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise _internal_error(exc) from exc


@router.post("/login", response_model=TokenPairResponse)
async def login(
    body: LoginRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenPairResponse:
    try:
        return await auth_service.login(body, _build_context(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_RATE_LIMIT_DETAIL
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_LOGIN_DENIED_DETAIL
        ) from exc
    except Exception as exc:
        raise _internal_error(exc) from exc


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    body: RefreshRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenPairResponse:
    try:
        return await auth_service.refresh(body, _build_context(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_RATE_LIMIT_DETAIL
        ) from exc
    except (RefreshReuseDetectedError, SessionRevokedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_REFRESH_DENIED_DETAIL
        ) from exc
    except Exception as exc:
        raise _internal_error(exc) from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """Idempotent: an unknown or already-revoked refresh token is not an error."""
    try:
        await auth_service.logout(body, _build_context(request))
    except Exception as exc:
        raise _internal_error(exc) from exc


@router.get("/me", response_model=MeResponse)
async def me(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> MeResponse:
    try:
        return await auth_service.get_me(principal.user_id)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        ) from exc
    except Exception as exc:
        raise _internal_error(exc) from exc
