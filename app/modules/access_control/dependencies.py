"""FastAPI dependency providers: authentication and case-scoped authorization.

The PostgreSQL engine and Redis client are created once, at import time --
mirroring `app/main.py` constructing `Settings` once at import time.
`create_async_engine`/`redis.asyncio.from_url` are both lazy and open no
real connection until first used, so this cannot fail import even if
neither service is reachable yet (readiness is `/readyz`'s job).

These dependencies -- `require_authenticated_user`, `require_case_action`,
and the `require_case_read`/`require_evidence_read`/`require_graph_read`/
`require_review_decision` convenience wrappers -- are the integration
points Nipun's later case/evidence endpoints and Shreshtha's graph
endpoints depend on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import redis.asyncio as redis
import sqlalchemy as sa
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event_safely
from app.modules.access_control.errors import InvalidTokenError
from app.modules.access_control.models import (
    AuditOutcome,
    AuthenticatedPrincipal,
    AuthorizedCasePrincipal,
    CaseAction,
)
from app.modules.access_control.policy import authorize_case_action
from app.modules.access_control.rate_limit import RateLimiter, RedisRateLimiter
from app.modules.access_control.repository import AccessControlRepository, create_engine
from app.modules.access_control.service import AuthService
from app.modules.access_control.tokens import decode_access_token

_settings = get_settings()
_engine = create_engine(_settings)
_repository = AccessControlRepository(_engine)
_redis_client: redis.Redis = redis.from_url(str(_settings.redis_url))
_login_rate_limiter: RateLimiter = RedisRateLimiter(_redis_client)
_refresh_rate_limiter: RateLimiter = RedisRateLimiter(_redis_client)

_bearer_scheme = HTTPBearer(auto_error=False)
logger = structlog.get_logger(__name__)

_INVALID_TOKEN_DETAIL = "invalid or expired access token"


def get_access_control_repository() -> AccessControlRepository:
    return _repository


def get_login_rate_limiter() -> RateLimiter:
    return _login_rate_limiter


def get_refresh_rate_limiter() -> RateLimiter:
    return _refresh_rate_limiter


def get_auth_service(
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    login_rate_limiter: Annotated[RateLimiter, Depends(get_login_rate_limiter)],
    refresh_rate_limiter: Annotated[RateLimiter, Depends(get_refresh_rate_limiter)],
) -> AuthService:
    return AuthService(
        repository=repository,
        login_rate_limiter=login_rate_limiter,
        refresh_rate_limiter=refresh_rate_limiter,
        jwt_secret=settings.auth_jwt_secret.get_secret_value(),
        jwt_algorithm=settings.auth_jwt_algorithm,
        jwt_issuer=settings.auth_jwt_issuer,
        jwt_audience=settings.auth_jwt_audience,
        access_token_ttl_seconds=settings.auth_access_token_ttl_seconds,
        refresh_token_ttl_seconds=settings.auth_refresh_token_ttl_seconds,
        login_rate_limit=settings.auth_login_rate_limit,
        refresh_rate_limit=settings.auth_refresh_rate_limit,
    )


async def require_authenticated_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedPrincipal:
    """Validate the `Authorization: Bearer <access token>` header against a live session.

    Beyond JWT signature/issuer/audience/expiry/type validation, this also
    checks the session the token references hasn't been revoked (so logout
    takes effect immediately, not just at token expiry) and that the user
    is still active -- both live database reads, deliberately never cached
    in the token itself (see `docs/decisions/ADR-003-...md`).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(
            credentials.credentials,
            secret=settings.auth_jwt_secret.get_secret_value(),
            algorithm=settings.auth_jwt_algorithm,
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience,
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_TOKEN_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        session = await repository.get_session_by_id(claims.sid)
        user = await repository.get_user_by_id(claims.sub) if session is not None else None
    except sa.exc.SQLAlchemyError as exc:
        logger.warning(
            "authorization.authentication_dependency_unavailable",
            request_id=get_request_id() or None,
            principal_ref=str(claims.sub),
            exc_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service temporarily unavailable",
        ) from exc

    if session is None or session.revoked_at is not None or session.user_id != claims.sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_TOKEN_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_TOKEN_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedPrincipal(user_id=claims.sub, session_id=claims.sid)


def require_case_action(
    action: CaseAction,
) -> Callable[..., Awaitable[AuthorizedCasePrincipal]]:
    """Build a dependency enforcing `action` against the `case_id` path parameter.

    Every failure mode -- case doesn't exist, no membership, inactive
    membership, insufficient role, insufficient clearance -- returns the
    exact same generic `403`, by design (default-deny; see `policy.py`).
    """

    async def _dependency(
        case_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
        repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    ) -> AuthorizedCasePrincipal:
        try:
            membership = await repository.get_active_membership(case_id, principal.user_id)
            case = await repository.get_case(case_id)
        except sa.exc.SQLAlchemyError as exc:
            logger.warning(
                "authorization.case_dependency_unavailable",
                request_id=get_request_id() or None,
                principal_ref=str(principal.user_id),
                requested_case_id=str(case_id),
                action=action.value,
                exc_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authorization service temporarily unavailable",
            ) from exc
        allowed = authorize_case_action(
            case_id=case_id,
            action=action,
            # `require_authenticated_user` already enforced this for the
            # current request -- see its docstring -- so it is never
            # re-queried here.
            user_is_active=True,
            membership=membership,
            case=case,
        )
        if not allowed or membership is None:
            await record_audit_event_safely(
                repository,
                event_type="case_access_denied",
                outcome=AuditOutcome.DENIED,
                now=datetime.now(UTC),
                request_id=get_request_id() or None,
                user_id=principal.user_id,
                case_id=case_id,
                metadata={"action": action.value},
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")
        # The security audit is deliberately independent of the access decision:
        # a telemetry outage cannot turn an allowed request into a denial.  The
        # record contains only the policy action and identifiers, never route
        # parameters or evidence/graph payloads.
        await record_audit_event_safely(
            repository,
            event_type="case_access_granted",
            outcome=AuditOutcome.SUCCESS,
            now=datetime.now(UTC),
            request_id=get_request_id() or None,
            user_id=principal.user_id,
            case_id=case_id,
            metadata={"action": action.value},
        )
        return AuthorizedCasePrincipal(
            principal=principal,
            case_id=case_id,
            action=action,
            membership=membership,
        )

    return _dependency


require_case_read = require_case_action(CaseAction.CASE_READ)
require_evidence_read = require_case_action(CaseAction.EVIDENCE_READ)
require_graph_read = require_case_action(CaseAction.GRAPH_READ)
require_integrity_read = require_case_action(CaseAction.INTEGRITY_READ)
require_integrity_verify = require_case_action(CaseAction.INTEGRITY_VERIFY)
require_integrity_export = require_case_action(CaseAction.INTEGRITY_EXPORT)
require_review_decision = require_case_action(CaseAction.REVIEW_DECIDE)
