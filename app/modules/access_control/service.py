"""Endpoint-facing orchestration: register / login / refresh / logout / me.

Combines `password.py`, `tokens.py`, `sessions.py`, `repository.py`,
`rate_limit.py`, and `audit.py` into the exact flows `api.py` calls. No
FastAPI/HTTP concerns live here -- `api.py` maps the typed errors this
module raises onto HTTP responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.errors import (
    AuthenticationError,
    RateLimitExceededError,
    RefreshReuseDetectedError,
    SessionRevokedError,
    ValidationError,
)
from app.modules.access_control.models import (
    AuditOutcome,
    CaseMembershipView,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    PublicUser,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserRecord,
)
from app.modules.access_control.password import hash_password, verify_password
from app.modules.access_control.rate_limit import RateLimiter, hash_rate_limit_key
from app.modules.access_control.repository import AccessControlRepository
from app.modules.access_control.sessions import (
    create_session,
    revoke_session_by_refresh_token,
    rotate_session,
)
from app.modules.access_control.tokens import create_access_token

# Hashed once at import time and reused for every "unknown email" / "inactive
# user" login attempt, so verifying against it costs the same as a real
# `verify_password` call against a real stored hash. Without this, an
# unknown-email login would skip Argon2id entirely and return measurably
# faster than a wrong-password login against a real account -- a timing
# side channel that would let a caller enumerate registered emails purely
# from response latency, even though the response *body* is identical.
_DUMMY_PASSWORD_HASH = hash_password("this-is-never-a-real-account-password")


@dataclass(frozen=True)
class RequestContext:
    """Per-request, non-secret context threaded through to audit events."""

    now: datetime
    request_id: str | None
    ip_marker: str | None


def _public_user(user: UserRecord) -> PublicUser:
    return PublicUser(
        user_id=user.user_id,
        email_normalized=user.email_normalized,
        display_name=user.display_name,
        is_active=user.is_active,
        created_at=user.created_at,
    )


class AuthService:
    """Stateless orchestration over an injected repository, limiters, and JWT config."""

    def __init__(
        self,
        *,
        repository: AccessControlRepository,
        login_rate_limiter: RateLimiter,
        refresh_rate_limiter: RateLimiter,
        jwt_secret: str,
        jwt_algorithm: str,
        jwt_issuer: str,
        jwt_audience: str,
        access_token_ttl_seconds: int,
        refresh_token_ttl_seconds: int,
        login_rate_limit: int,
        refresh_rate_limit: int,
    ) -> None:
        self._repository = repository
        self._login_rate_limiter = login_rate_limiter
        self._refresh_rate_limiter = refresh_rate_limiter
        self._jwt_secret = jwt_secret
        self._jwt_algorithm = jwt_algorithm
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience
        self._access_token_ttl_seconds = access_token_ttl_seconds
        self._refresh_token_ttl_seconds = refresh_token_ttl_seconds
        self._login_rate_limit = login_rate_limit
        self._refresh_rate_limit = refresh_rate_limit

    def _issue_access_token(self, *, user_id: UUID, session_id: UUID, now: datetime) -> str:
        return create_access_token(
            user_id=user_id,
            session_id=session_id,
            secret=self._jwt_secret,
            algorithm=self._jwt_algorithm,
            issuer=self._jwt_issuer,
            audience=self._jwt_audience,
            ttl_seconds=self._access_token_ttl_seconds,
            now=now,
        )

    async def register(self, request: RegisterRequest, ctx: RequestContext) -> PublicUser:
        """Register a new user. Never grants elevated privileges automatically."""
        existing = await self._repository.get_user_by_email(request.email)
        if existing is not None:
            raise ValidationError("email already registered")

        user = UserRecord(
            user_id=uuid4(),
            email_normalized=request.email,
            display_name=request.display_name,
            password_hash=hash_password(request.password),
            is_active=True,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        await self._repository.create_user(user)
        await record_audit_event(
            self._repository,
            event_type="auth.register",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=user.user_id,
            ip_marker=ctx.ip_marker,
        )
        return _public_user(user)

    async def login(self, request: LoginRequest, ctx: RequestContext) -> TokenPairResponse:
        """Authenticate.

        Raises `RateLimitExceededError` or `AuthenticationError` (generic denial).
        """
        rate_limit_key = hash_rate_limit_key("login", request.email)
        allowed = await self._login_rate_limiter.check_and_increment(
            rate_limit_key, limit=self._login_rate_limit
        )
        if not allowed:
            await record_audit_event(
                self._repository,
                event_type="auth.login.rate_limited",
                outcome=AuditOutcome.DENIED,
                now=ctx.now,
                request_id=ctx.request_id,
                ip_marker=ctx.ip_marker,
            )
            raise RateLimitExceededError("too many login attempts")

        user = await self._repository.get_user_by_email(request.email)
        if user is not None and user.is_active:
            password_ok = verify_password(request.password, user.password_hash)
        else:
            # Burn the same Argon2id cost as a real attempt -- see
            # `_DUMMY_PASSWORD_HASH` -- so response timing can't reveal
            # whether this email is registered/active.
            verify_password(request.password, _DUMMY_PASSWORD_HASH)
            password_ok = False

        if user is None or not user.is_active or not password_ok:
            await record_audit_event(
                self._repository,
                event_type="auth.login.failure",
                outcome=AuditOutcome.FAILURE,
                now=ctx.now,
                request_id=ctx.request_id,
                user_id=user.user_id if user is not None else None,
                ip_marker=ctx.ip_marker,
            )
            raise AuthenticationError("invalid email or password")

        issued = await create_session(
            self._repository,
            user_id=user.user_id,
            now=ctx.now,
            ttl_seconds=self._refresh_token_ttl_seconds,
        )
        access_token = self._issue_access_token(
            user_id=user.user_id, session_id=issued.session.session_id, now=ctx.now
        )
        await record_audit_event(
            self._repository,
            event_type="auth.login.success",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=user.user_id,
            ip_marker=ctx.ip_marker,
        )
        return TokenPairResponse(
            access_token=access_token,
            refresh_token=issued.refresh_token,
            expires_in=self._access_token_ttl_seconds,
        )

    async def refresh(self, request: RefreshRequest, ctx: RequestContext) -> TokenPairResponse:
        """Rotate a refresh token.

        Raises `RateLimitExceededError`, `RefreshReuseDetectedError`, or `SessionRevokedError`.
        """
        # Keyed by client IP marker, never the refresh token itself: a
        # refresh token is single-use and rotates on every successful call
        # (see `rotate_session`), so a legitimate client's own repeated
        # calls each present a *different* token string. Keying on the
        # token value therefore let every attempt land in its own
        # one-shot bucket and never accumulate -- an authenticated client
        # exceeding the configured limit could never actually observe a
        # `429` (P5-REGRESSION-AUTH-001). `ip_marker` is the same
        # pre-validation, stable-per-caller identity `login`'s own rate
        # limit already keys on (there, `request.email`) -- known before
        # the token is looked up or rotated, so the check runs unchanged
        # ahead of any session mutation. A caller with no discoverable
        # client host (`ip_marker is None`, e.g. certain test transports)
        # shares one fixed bucket rather than bypassing the limit entirely.
        rate_limit_key = hash_rate_limit_key("refresh", ctx.ip_marker or "unknown")
        allowed = await self._refresh_rate_limiter.check_and_increment(
            rate_limit_key, limit=self._refresh_rate_limit
        )
        if not allowed:
            await record_audit_event(
                self._repository,
                event_type="auth.refresh.rate_limited",
                outcome=AuditOutcome.DENIED,
                now=ctx.now,
                request_id=ctx.request_id,
                ip_marker=ctx.ip_marker,
            )
            raise RateLimitExceededError("too many refresh attempts")

        try:
            issued = await rotate_session(
                self._repository,
                refresh_token=request.refresh_token,
                now=ctx.now,
                ttl_seconds=self._refresh_token_ttl_seconds,
            )
        except RefreshReuseDetectedError:
            await record_audit_event(
                self._repository,
                event_type="auth.refresh.reuse_detected",
                outcome=AuditOutcome.DENIED,
                now=ctx.now,
                request_id=ctx.request_id,
                ip_marker=ctx.ip_marker,
            )
            raise
        except SessionRevokedError:
            await record_audit_event(
                self._repository,
                event_type="auth.refresh.denied",
                outcome=AuditOutcome.DENIED,
                now=ctx.now,
                request_id=ctx.request_id,
                ip_marker=ctx.ip_marker,
            )
            raise

        access_token = self._issue_access_token(
            user_id=issued.session.user_id, session_id=issued.session.session_id, now=ctx.now
        )
        await record_audit_event(
            self._repository,
            event_type="auth.refresh.success",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=issued.session.user_id,
            ip_marker=ctx.ip_marker,
        )
        return TokenPairResponse(
            access_token=access_token,
            refresh_token=issued.refresh_token,
            expires_in=self._access_token_ttl_seconds,
        )

    async def logout(self, request: LogoutRequest, ctx: RequestContext) -> None:
        """Revoke the session behind this refresh token. Safe to call repeatedly (idempotent)."""
        await revoke_session_by_refresh_token(
            self._repository, refresh_token=request.refresh_token, now=ctx.now
        )
        await record_audit_event(
            self._repository,
            event_type="auth.logout",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            ip_marker=ctx.ip_marker,
        )

    async def get_me(self, user_id: UUID) -> MeResponse:
        """Public identity + active case memberships for an already-authenticated user."""
        user = await self._repository.get_user_by_id(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("account is no longer active")
        memberships = await self._repository.list_active_memberships_for_user(user_id)
        return MeResponse(
            user=_public_user(user),
            case_memberships=tuple(
                CaseMembershipView(
                    case_id=m.case_id,
                    role=m.role,
                    clearance=m.clearance,
                    is_active=m.is_active,
                )
                for m in memberships
            ),
        )
