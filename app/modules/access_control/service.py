"""Endpoint-facing orchestration: register / login / refresh / logout / me.

Combines `password.py`, `tokens.py`, `sessions.py`, `repository.py`,
`rate_limit.py`, and `audit.py` into the exact flows `api.py` calls. No
FastAPI/HTTP concerns live here -- `api.py` maps the typed errors this
module raises onto HTTP responses.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.errors import (
    AuthenticationError,
    InvalidTokenError,
    RateLimitExceededError,
    RefreshReuseDetectedError,
    SessionRevokedError,
    ValidationError,
)
from app.modules.access_control.models import (
    AuditOutcome,
    CaseMembershipView,
    ChangePasswordRequest,
    CredentialResetResponse,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    MfaEnrollResponse,
    MfaLoginVerifyRequest,
    ProvisionCaseHeadRequest,
    PublicUser,
    RefreshRequest,
    SystemRole,
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
from app.modules.access_control.tokens import (
    create_access_token,
    create_mfa_challenge_token,
    decode_mfa_challenge_token,
)
from app.modules.access_control.totp import generate_totp_secret, totp_provisioning_uri, verify_totp

#: Length of a generated one-time temporary password (`reset_credentials`).
#: `secrets.token_urlsafe(n)` yields ~4n/3 characters -- comfortably inside
#: `password.py`'s `MIN_PASSWORD_LENGTH`..`MAX_PASSWORD_LENGTH` bounds.
_TEMPORARY_PASSWORD_BYTES = 16

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
        system_role=user.system_role,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
    )


class AuthService:
    """Stateless orchestration over an injected repository, limiters, and JWT config."""

    def __init__(
        self,
        *,
        repository: AccessControlRepository,
        login_rate_limiter: RateLimiter,
        refresh_rate_limiter: RateLimiter,
        mfa_rate_limiter: RateLimiter,
        jwt_secret: str,
        jwt_algorithm: str,
        jwt_issuer: str,
        jwt_audience: str,
        access_token_ttl_seconds: int,
        refresh_token_ttl_seconds: int,
        mfa_challenge_ttl_seconds: int,
        login_rate_limit: int,
        refresh_rate_limit: int,
        mfa_rate_limit: int,
    ) -> None:
        self._repository = repository
        self._login_rate_limiter = login_rate_limiter
        self._refresh_rate_limiter = refresh_rate_limiter
        self._mfa_rate_limiter = mfa_rate_limiter
        self._jwt_secret = jwt_secret
        self._jwt_algorithm = jwt_algorithm
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience
        self._access_token_ttl_seconds = access_token_ttl_seconds
        self._refresh_token_ttl_seconds = refresh_token_ttl_seconds
        self._mfa_challenge_ttl_seconds = mfa_challenge_ttl_seconds
        self._login_rate_limit = login_rate_limit
        self._refresh_rate_limit = refresh_rate_limit
        self._mfa_rate_limit = mfa_rate_limit

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

    def _issue_mfa_challenge_token(self, *, user_id: UUID, now: datetime) -> str:
        return create_mfa_challenge_token(
            user_id=user_id,
            secret=self._jwt_secret,
            algorithm=self._jwt_algorithm,
            issuer=self._jwt_issuer,
            audience=self._jwt_audience,
            ttl_seconds=self._mfa_challenge_ttl_seconds,
            now=now,
        )

    async def _establish_session(self, *, user_id: UUID, now: datetime) -> TokenPairResponse:
        """Shared tail of both login paths: create a session, issue tokens, `mfa_required=False`."""
        issued = await create_session(
            self._repository, user_id=user_id, now=now, ttl_seconds=self._refresh_token_ttl_seconds
        )
        access_token = self._issue_access_token(
            user_id=user_id, session_id=issued.session.session_id, now=now
        )
        return TokenPairResponse(
            access_token=access_token,
            refresh_token=issued.refresh_token,
            expires_in=self._access_token_ttl_seconds,
        )

    async def provision_case_head(
        self,
        request: ProvisionCaseHeadRequest,
        ctx: RequestContext,
        *,
        provisioned_by: UUID,
    ) -> PublicUser:
        """Provisioner-only Case Head provisioning; never public signup."""
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
            system_role=SystemRole.CASE_HEAD,
            # Every provisioned account must change this password and
            # enroll MFA before it can be treated as fully onboarded (see
            # ADR-033) -- never optional, never a request field the
            # Provisioner can turn off.
            must_change_password=True,
        )
        await self._repository.create_user(user)
        await record_audit_event(
            self._repository,
            event_type="provisioner.create_case_head",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=provisioned_by,
            ip_marker=ctx.ip_marker,
            metadata={
                "provisioned_user_id": str(user.user_id),
                "organization_role": SystemRole.CASE_HEAD.value,
            },
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

        if user.totp_enabled:
            # Password verified, but MFA is enrolled: no session yet -- only
            # a short-lived ticket good for one shot at `mfa/login-verify`.
            mfa_token = self._issue_mfa_challenge_token(user_id=user.user_id, now=ctx.now)
            await record_audit_event(
                self._repository,
                event_type="auth.mfa.login_challenge",
                outcome=AuditOutcome.SUCCESS,
                now=ctx.now,
                request_id=ctx.request_id,
                user_id=user.user_id,
                ip_marker=ctx.ip_marker,
            )
            return TokenPairResponse(mfa_required=True, mfa_token=mfa_token)

        tokens = await self._establish_session(user_id=user.user_id, now=ctx.now)
        await record_audit_event(
            self._repository,
            event_type="auth.login.success",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=user.user_id,
            ip_marker=ctx.ip_marker,
        )
        return tokens

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

    async def verify_mfa_login(
        self, request: MfaLoginVerifyRequest, ctx: RequestContext
    ) -> TokenPairResponse:
        """The second step of a two-factor login: exchange a valid `mfa_token` + code for a session.

        Rate-limited by `ip_marker`, exactly like `refresh` -- a 6-digit
        code has only 1e6 possibilities, so this must stay tight regardless
        of which account is being targeted. Raises `RateLimitExceededError`
        or `AuthenticationError` (generic denial: an expired/malformed
        token and a wrong code are indistinguishable from the outside).
        """
        rate_limit_key = hash_rate_limit_key("mfa_login", ctx.ip_marker or "unknown")
        allowed = await self._mfa_rate_limiter.check_and_increment(
            rate_limit_key, limit=self._mfa_rate_limit
        )
        if not allowed:
            await record_audit_event(
                self._repository,
                event_type="auth.mfa.login_rate_limited",
                outcome=AuditOutcome.DENIED,
                now=ctx.now,
                request_id=ctx.request_id,
                ip_marker=ctx.ip_marker,
            )
            raise RateLimitExceededError("too many authentication code attempts")

        try:
            claims = decode_mfa_challenge_token(
                request.mfa_token,
                secret=self._jwt_secret,
                algorithm=self._jwt_algorithm,
                issuer=self._jwt_issuer,
                audience=self._jwt_audience,
            )
        except InvalidTokenError as exc:
            raise AuthenticationError("invalid or expired authentication challenge") from exc

        user = await self._repository.get_user_by_id(claims.sub)
        if (
            user is None
            or not user.is_active
            or not user.totp_enabled
            or user.totp_secret is None
            or not verify_totp(user.totp_secret, request.code)
        ):
            await record_audit_event(
                self._repository,
                event_type="auth.mfa.login_failure",
                outcome=AuditOutcome.FAILURE,
                now=ctx.now,
                request_id=ctx.request_id,
                user_id=user.user_id if user is not None else None,
                ip_marker=ctx.ip_marker,
            )
            raise AuthenticationError("invalid authentication code")

        tokens = await self._establish_session(user_id=user.user_id, now=ctx.now)
        await record_audit_event(
            self._repository,
            event_type="auth.login.success",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=user.user_id,
            ip_marker=ctx.ip_marker,
        )
        return tokens

    async def enroll_mfa(self, user_id: UUID, ctx: RequestContext) -> MfaEnrollResponse:
        """(Re)start TOTP enrollment: generate a fresh secret, not yet enabled.

        Safe to call again before `confirm_mfa_enrollment` (e.g. the
        investigator's first attempt QR didn't scan) -- each call overwrites
        whatever secret was pending; `totp_enabled` only ever flips to
        `True` inside `confirm_mfa_enrollment`.
        """
        user = await self._repository.get_user_by_id(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("account is no longer active")
        secret = generate_totp_secret()
        await self._repository.update_totp(
            user_id, totp_secret=secret, totp_enabled=False, updated_at=ctx.now
        )
        return MfaEnrollResponse(
            secret=secret,
            provisioning_uri=totp_provisioning_uri(
                secret=secret, account_name=user.email_normalized
            ),
        )

    async def confirm_mfa_enrollment(
        self, user_id: UUID, code: str, ctx: RequestContext
    ) -> PublicUser:
        """Confirm enrollment with one real code from the authenticator app.

        Raises `ValidationError` if no enrollment is pending, or
        `AuthenticationError` for a wrong code (mirrors `verify_mfa_login`'s
        generic-denial shape).
        """
        user = await self._repository.get_user_by_id(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("account is no longer active")
        if user.totp_secret is None:
            raise ValidationError("no MFA enrollment is pending for this account")
        if not verify_totp(user.totp_secret, code):
            await record_audit_event(
                self._repository,
                event_type="auth.mfa.enroll_failure",
                outcome=AuditOutcome.FAILURE,
                now=ctx.now,
                request_id=ctx.request_id,
                user_id=user_id,
                ip_marker=ctx.ip_marker,
            )
            raise AuthenticationError("invalid authentication code")
        await self._repository.update_totp(
            user_id, totp_secret=user.totp_secret, totp_enabled=True, updated_at=ctx.now
        )
        await record_audit_event(
            self._repository,
            event_type="auth.mfa.enrolled",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=user_id,
            ip_marker=ctx.ip_marker,
        )
        updated = await self._repository.get_user_by_id(user_id)
        assert updated is not None  # just written above, in the same repository
        return _public_user(updated)

    async def change_password(
        self, user_id: UUID, request: ChangePasswordRequest, ctx: RequestContext
    ) -> None:
        """Self-service password change -- clears `must_change_password` on success.

        Raises `AuthenticationError` if the current password is wrong (the
        account itself, never the reason, is what the HTTP layer maps to a
        generic 401 -- consistent with every other credential check in this
        module).
        """
        user = await self._repository.get_user_by_id(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("account is no longer active")
        if not verify_password(request.current_password, user.password_hash):
            await record_audit_event(
                self._repository,
                event_type="auth.password_change_failure",
                outcome=AuditOutcome.FAILURE,
                now=ctx.now,
                request_id=ctx.request_id,
                user_id=user_id,
                ip_marker=ctx.ip_marker,
            )
            raise AuthenticationError("current password is incorrect")
        await self._repository.update_password(
            user_id,
            password_hash=hash_password(request.new_password),
            must_change_password=False,
            updated_at=ctx.now,
        )
        await record_audit_event(
            self._repository,
            event_type="auth.password_changed",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=user_id,
            ip_marker=ctx.ip_marker,
        )

    async def reset_credentials(
        self, user_id: UUID, ctx: RequestContext, *, reset_by: UUID
    ) -> CredentialResetResponse:
        """Reissue temporary credentials after the caller was authorized.

        Reissues a fresh one-time temporary password, clears TOTP enrollment
        entirely (the investigator re-enrolls from scratch -- there is no
        such thing as "trust the old secret a little"), forces a password
        change on next login, and immediately revokes every live session
        for the affected account so a still-valid old token can't outlive
        the reset.
        """
        user = await self._repository.get_user_by_id(user_id)
        if user is None:
            raise ValidationError("user not found")
        temporary_password = secrets.token_urlsafe(_TEMPORARY_PASSWORD_BYTES)
        await self._repository.update_password(
            user_id,
            password_hash=hash_password(temporary_password),
            must_change_password=True,
            updated_at=ctx.now,
        )
        await self._repository.update_totp(
            user_id, totp_secret=None, totp_enabled=False, updated_at=ctx.now
        )
        await self._repository.revoke_all_sessions_for_user(user_id, ctx.now)
        await record_audit_event(
            self._repository,
            event_type="account.reset_credentials",
            outcome=AuditOutcome.SUCCESS,
            now=ctx.now,
            request_id=ctx.request_id,
            user_id=reset_by,
            ip_marker=ctx.ip_marker,
            metadata={"reset_user_id": str(user_id)},
        )
        return CredentialResetResponse(temporary_password=temporary_password)
