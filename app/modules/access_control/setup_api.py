"""HTTP endpoints for opt-in, one-time private deployment setup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.core.errors import get_request_id
from app.modules.access_control.audit import hash_ip
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_first_admin_setup_rate_limiter,
)
from app.modules.access_control.models import (
    FirstAdminSetupRequest,
    FirstAdminSetupStatusResponse,
    PublicUser,
)
from app.modules.access_control.rate_limit import RateLimiter, hash_rate_limit_key
from app.modules.access_control.repository import AccessControlRepository
from app.modules.access_control.setup_service import (
    create_first_admin,
    first_admin_setup_required,
    setup_is_available,
    token_matches,
)

router = APIRouter(prefix="/api/v1/setup/first-admin", tags=["setup"])
_SETUP_UNAVAILABLE_DETAIL = "first-admin setup is not available"
_SETUP_DENIED_DETAIL = "invalid setup credentials"
_RATE_LIMIT_DETAIL = "too many attempts, try again later"


def _configured_token(settings: Settings) -> str | None:
    return (
        settings.first_admin_setup_token.get_secret_value()
        if settings.first_admin_setup_token is not None
        else None
    )


def _client_marker(request: Request) -> str:
    return hash_ip(request.client.host if request.client else "unknown")


@router.get("/status", response_model=FirstAdminSetupStatusResponse)
async def first_admin_status(
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FirstAdminSetupStatusResponse:
    return FirstAdminSetupStatusResponse(
        setup_required=await first_admin_setup_required(
            repository,
            enabled=settings.first_admin_setup_enabled,
            token=_configured_token(settings),
        )
    )


@router.post("", response_model=PublicUser, status_code=status.HTTP_201_CREATED)
async def setup_first_admin(
    body: FirstAdminSetupRequest,
    request: Request,
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RateLimiter, Depends(get_first_admin_setup_rate_limiter)],
    setup_token: Annotated[str | None, Header(alias="X-TraceX-First-Admin-Setup-Token")] = None,
) -> PublicUser:
    expected = _configured_token(settings)
    if expected is None or not setup_is_available(
        enabled=settings.first_admin_setup_enabled, token=expected
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SETUP_UNAVAILABLE_DETAIL)
    rate_key = hash_rate_limit_key("first_admin_setup", _client_marker(request))
    if not await limiter.check_and_increment(rate_key, limit=settings.first_admin_setup_rate_limit):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_RATE_LIMIT_DETAIL
        )
    if not token_matches(expected=expected, submitted=setup_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_SETUP_DENIED_DETAIL)
    if not await first_admin_setup_required(
        repository, enabled=settings.first_admin_setup_enabled, token=expected
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_SETUP_UNAVAILABLE_DETAIL)
    created = await create_first_admin(
        repository,
        body,
        now=datetime.now(UTC),
        request_id=get_request_id(),
        ip_marker=_client_marker(request),
    )
    if created is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_SETUP_UNAVAILABLE_DETAIL)
    return created


__all__ = ["router"]
