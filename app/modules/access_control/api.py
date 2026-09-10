"""Auth HTTP layer: the `/api/v1/auth` router and the security-headers middleware.

Endpoints are thin: parse the request, call `AuthService`, translate typed
errors from `errors.py` into safe, generic HTTP responses. No business
logic lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_RATE_LIMIT_DETAIL = "too many attempts, try again later"
_LOGIN_DENIED_DETAIL = "invalid email or password"
_REFRESH_DENIED_DETAIL = "invalid or expired refresh token"

#: Paths that carry credentials/tokens and must never be cached by a
#: client, proxy, or browser history.
_NO_STORE_PATH_PREFIX = "/api/v1/auth"


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: safe, static security headers on every normal response.

    Implemented as raw ASGI, not `starlette.middleware.base.BaseHTTPMiddleware`:
    this only needs to mutate outgoing response headers, which a `send`
    wrapper does directly. (Genuinely unexpected exceptions are handled
    separately -- see `app.core.errors`'s module docstring for why *no*
    user-added middleware, pure ASGI or otherwise, ever sees the response
    Starlette's own generic-`Exception` handler builds, and why that
    handler sets these same headers itself instead of relying on this
    middleware.)

    Deliberately does not implement CORS: Phase 1 has no browser frontend
    consumer, so no `Access-Control-Allow-Origin` is set at all (never
    `*`) -- see `docs/architecture/security-boundaries-v1.md`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                if path.startswith(_NO_STORE_PATH_PREFIX):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_wrapper)


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


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    """Idempotent: an unknown or already-revoked refresh token is not an error."""
    await auth_service.logout(body, _build_context(request))


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
