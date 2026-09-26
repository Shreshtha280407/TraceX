"""Auth HTTP layer: the `/api/v1/auth` router and the security-headers middleware.

Endpoints are thin: parse the request, call `AuthService`, translate typed
errors from `errors.py` into safe, generic HTTP responses. No business
logic lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import get_request_id
from app.modules.access_control.audit import hash_ip, record_audit_event
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_auth_service,
    require_authenticated_user,
    require_provisioner,
)
from app.modules.access_control.errors import (
    AuthenticationError,
    RateLimitExceededError,
    RefreshReuseDetectedError,
    SessionRevokedError,
    ValidationError,
)
from app.modules.access_control.models import (
    AuditOutcome,
    AuthenticatedPrincipal,
    CaseHeadStatusUpdateRequest,
    ChangePasswordRequest,
    CredentialResetResponse,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    MfaEnrollResponse,
    MfaLoginVerifyRequest,
    MfaVerifyRequest,
    ProvisionCaseHeadRequest,
    PublicUser,
    PublicUserListResponse,
    RefreshRequest,
    SystemRole,
    TokenPairResponse,
)
from app.modules.access_control.repository import AccessControlRepository
from app.modules.access_control.service import AuthService, RequestContext

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
provisioning_router = APIRouter(prefix="/api/v1/provisioning", tags=["provisioning"])

_RATE_LIMIT_DETAIL = "too many attempts, try again later"
_LOGIN_DENIED_DETAIL = "invalid email or password"
_REFRESH_DENIED_DETAIL = "invalid or expired refresh token"

#: Paths that carry credentials/tokens and must never be cached by a
#: client, proxy, or browser history.
_NO_STORE_PATH_PREFIXES = ("/api/v1/auth", "/api/v1/setup")


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
                if path.startswith(_NO_STORE_PATH_PREFIXES):
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


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
async def mfa_enroll(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> MfaEnrollResponse:
    try:
        return await auth_service.enroll_mfa(principal.user_id, _build_context(request))
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        ) from exc


@router.post("/mfa/verify", response_model=PublicUser)
async def mfa_verify(
    body: MfaVerifyRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> PublicUser:
    try:
        return await auth_service.confirm_mfa_enrollment(
            principal.user_id, body.code, _build_context(request)
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authentication code"
        ) from exc


@router.post("/mfa/login-verify", response_model=TokenPairResponse)
async def mfa_login_verify(
    body: MfaLoginVerifyRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenPairResponse:
    try:
        return await auth_service.verify_mfa_login(body, _build_context(request))
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_RATE_LIMIT_DETAIL
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired authentication code",
        ) from exc


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    try:
        await auth_service.change_password(principal.user_id, body, _build_context(request))
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="current password is incorrect"
        ) from exc


@provisioning_router.post(
    "/case-heads", response_model=PublicUser, status_code=status.HTTP_201_CREATED
)
async def provision_case_head(
    body: ProvisionCaseHeadRequest,
    request: Request,
    provisioner: Annotated[AuthenticatedPrincipal, Depends(require_provisioner)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> PublicUser:
    """Create exactly a Case Head; no public account provisioning exists."""
    try:
        return await auth_service.provision_case_head(
            body, _build_context(request), provisioned_by=provisioner.user_id
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@provisioning_router.get("/case-heads", response_model=PublicUserListResponse)
async def list_case_heads(
    provisioner: Annotated[AuthenticatedPrincipal, Depends(require_provisioner)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PublicUserListResponse:
    """Minimal Case Head account list; never a case or credential directory."""
    users = await repository.list_users_with_system_role(
        SystemRole.CASE_HEAD.value, limit=limit, offset=offset
    )
    return PublicUserListResponse(
        items=tuple(
            PublicUser(
                user_id=u.user_id,
                email_normalized=u.email_normalized,
                display_name=u.display_name,
                is_active=u.is_active,
                created_at=u.created_at,
                system_role=u.system_role,
                must_change_password=u.must_change_password,
                totp_enabled=u.totp_enabled,
            )
            for u in users
        )
    )


@provisioning_router.patch("/case-heads/{user_id}", response_model=PublicUser)
async def update_case_head_status(
    user_id: UUID,
    body: CaseHeadStatusUpdateRequest,
    request: Request,
    provisioner: Annotated[AuthenticatedPrincipal, Depends(require_provisioner)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
) -> PublicUser:
    """Activate/deactivate a Case Head only; other account classes are invisible here."""
    target = await repository.get_user_by_id(user_id)
    if target is None or target.system_role != SystemRole.CASE_HEAD:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case head not found")
    await repository.set_user_active(
        user_id, is_active=body.is_active, updated_at=datetime.now(UTC)
    )
    if not body.is_active:
        await repository.revoke_all_sessions_for_user(user_id, datetime.now(UTC))
    updated = await repository.get_user_by_id(user_id)
    assert updated is not None
    await record_audit_event(
        repository,
        event_type="provisioner.update_case_head",
        outcome=AuditOutcome.SUCCESS,
        now=datetime.now(UTC),
        request_id=get_request_id(),
        user_id=provisioner.user_id,
        ip_marker=_build_context(request).ip_marker,
        metadata={"case_head_user_id": str(user_id), "is_active": body.is_active},
    )
    return PublicUser(
        user_id=updated.user_id,
        email_normalized=updated.email_normalized,
        display_name=updated.display_name,
        is_active=updated.is_active,
        created_at=updated.created_at,
        system_role=updated.system_role,
        must_change_password=updated.must_change_password,
        totp_enabled=updated.totp_enabled,
    )


@provisioning_router.post(
    "/case-heads/{user_id}/reset-credentials", response_model=CredentialResetResponse
)
async def reset_case_head_credentials(
    user_id: UUID,
    request: Request,
    provisioner: Annotated[AuthenticatedPrincipal, Depends(require_provisioner)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> CredentialResetResponse:
    """Provisioners can recover Case Heads, never lower accounts through this route."""
    target = await repository.get_user_by_id(user_id)
    if target is None or target.system_role != SystemRole.CASE_HEAD:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case head not found")
    try:
        return await auth_service.reset_credentials(
            user_id, _build_context(request), reset_by=provisioner.user_id
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
