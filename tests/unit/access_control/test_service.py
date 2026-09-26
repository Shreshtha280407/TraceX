"""Scenarios 2-5 and general orchestration coverage for `AuthService`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.access_control.errors import (
    AuthenticationError,
    RateLimitExceededError,
    SessionRevokedError,
    ValidationError,
)
from app.modules.access_control.models import (
    CaseRole,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MfaLoginVerifyRequest,
    ProvisionCaseHeadRequest,
    RefreshRequest,
    SystemRole,
)
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.access_control.service import AuthService, RequestContext
from app.modules.access_control.totp import verify_totp
from tests.fixtures.access_control.factories import (
    DEFAULT_PASSWORD,
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
CTX = RequestContext(now=NOW, request_id="test-request-id", ip_marker=None)


def _real_ctx() -> RequestContext:
    """A context stamped with the real wall clock.

    Needed specifically for tests that mint an MFA-challenge JWT and then
    decode it: `decode_mfa_challenge_token`'s underlying `jwt.decode` checks
    `exp` against real wall-clock time, not against whatever fake `now` the
    token was minted with -- so a token minted against the fixed, long-past
    `CTX`/`NOW` above would already look expired by the time it's decoded.
    Plain business-logic tests that never round-trip through real JWT
    decoding are unaffected and keep using the fixed `CTX`.
    """
    return RequestContext(now=datetime.now(UTC), request_id="test-request-id", ip_marker=None)


def _current_totp_code(secret: str) -> str:
    """Compute a real, currently-valid code for `secret` -- test-only, mirrors RFC 6238."""
    import base64
    import hashlib
    import hmac
    import struct
    import time

    counter = int(time.time() // 30)
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % 1_000_000).zfill(6)


def _make_service(
    repository: FakeAccessControlRepository | None = None,
    *,
    login_rate_limit: int = 1000,
    refresh_rate_limit: int = 1000,
    mfa_rate_limit: int = 1000,
) -> tuple[AuthService, FakeAccessControlRepository]:
    repo = repository or FakeAccessControlRepository()
    service = AuthService(
        repository=repo,  # type: ignore[arg-type]
        login_rate_limiter=InMemoryRateLimiter(),
        refresh_rate_limiter=InMemoryRateLimiter(),
        mfa_rate_limiter=InMemoryRateLimiter(),
        jwt_secret="x" * 32,
        jwt_algorithm="HS256",
        jwt_issuer="tracex-api-test",
        jwt_audience="tracex-clients-test",
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=1_209_600,
        mfa_challenge_ttl_seconds=300,
        login_rate_limit=login_rate_limit,
        refresh_rate_limit=refresh_rate_limit,
        mfa_rate_limit=mfa_rate_limit,
    )
    return service, repo


# --- Scenario 2: registration rejects invalid data safely -------------------


def test_register_request_rejects_short_password() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic.ValidationError, not this module's
        ProvisionCaseHeadRequest(email="a@b.com", password="short", display_name="A")


def test_register_request_rejects_malformed_email() -> None:
    with pytest.raises(Exception):  # noqa: B017
        ProvisionCaseHeadRequest(email="not-an-email", password=DEFAULT_PASSWORD, display_name="A")


def test_register_request_normalizes_email() -> None:
    request = ProvisionCaseHeadRequest(
        email="  Someone@Example.COM  ", password=DEFAULT_PASSWORD, display_name="A"
    )
    assert request.email == "someone@example.com"


async def test_register_creates_a_user_and_never_returns_the_password_hash() -> None:
    service, repo = _make_service()
    request = ProvisionCaseHeadRequest(
        email="new@example.test", password=DEFAULT_PASSWORD, display_name="New"
    )
    public_user = await service.provision_case_head(request, CTX, provisioned_by=uuid4())

    assert public_user.email_normalized == "new@example.test"
    assert not hasattr(public_user, "password_hash")
    assert "password_hash" not in public_user.model_dump()
    stored = repo.users[public_user.user_id]
    assert stored.password_hash != DEFAULT_PASSWORD


async def test_register_rejects_duplicate_email() -> None:
    service, repo = _make_service()
    request = ProvisionCaseHeadRequest(
        email="dup@example.test", password=DEFAULT_PASSWORD, display_name="A"
    )
    await service.provision_case_head(request, CTX, provisioned_by=uuid4())
    with pytest.raises(ValidationError):
        await service.provision_case_head(request, CTX, provisioned_by=uuid4())


async def test_registered_user_is_never_automatically_privileged() -> None:
    # `system_role` defaults to `None` unless the (already-admin-gated,
    # per `dependencies.require_system_admin`) caller explicitly requests
    # one -- provisioning cannot grant case-level privilege either, since
    # that only ever comes from an explicit `case_memberships` row.
    service, repo = _make_service()
    request = ProvisionCaseHeadRequest(
        email="plain@example.test", password=DEFAULT_PASSWORD, display_name="A"
    )
    public_user = await service.provision_case_head(request, CTX, provisioned_by=uuid4())
    assert public_user.system_role is SystemRole.CASE_HEAD
    assert repo.memberships == {}


async def test_provisioner_can_create_case_head_only() -> None:
    service, repo = _make_service()
    request = ProvisionCaseHeadRequest(
        email="newadmin@example.test",
        password=DEFAULT_PASSWORD,
        display_name="A",
    )
    public_user = await service.provision_case_head(request, CTX, provisioned_by=uuid4())
    assert public_user.system_role is SystemRole.CASE_HEAD
    assert repo.users[public_user.user_id].system_role is SystemRole.CASE_HEAD


# --- Scenario 3: login success creates valid access and refresh tokens -----


async def test_login_success_returns_a_usable_token_pair() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user

    result = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), CTX
    )
    assert result.access_token
    assert result.refresh_token
    assert result.token_type == "bearer"
    assert result.expires_in == 900
    assert len(repo.sessions) == 1


# --- Scenario 4: login failure does not reveal whether the email exists ----


async def test_unknown_email_and_wrong_password_raise_the_identical_error() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user

    unknown_email_error = None
    wrong_password_error = None
    try:
        await service.login(LoginRequest(email="nobody@example.test", password="whatever12"), CTX)
    except AuthenticationError as exc:
        unknown_email_error = str(exc)
    try:
        await service.login(
            LoginRequest(email=user.email_normalized, password="totally-wrong-pw"), CTX
        )
    except AuthenticationError as exc:
        wrong_password_error = str(exc)

    assert unknown_email_error is not None
    assert unknown_email_error == wrong_password_error


# --- Scenario 5: disabled user cannot authenticate --------------------------


async def test_disabled_user_cannot_authenticate() -> None:
    service, repo = _make_service()
    user = make_user_record(is_active=False)
    repo.users[user.user_id] = user
    with pytest.raises(AuthenticationError):
        await service.login(
            LoginRequest(email=user.email_normalized, password="correct-horse-battery-staple"), CTX
        )


async def test_disabled_user_login_failure_matches_unknown_email_message() -> None:
    service, repo = _make_service()
    disabled = make_user_record(is_active=False)
    repo.users[disabled.user_id] = disabled

    disabled_error = None
    unknown_error = None
    try:
        await service.login(
            LoginRequest(email=disabled.email_normalized, password="correct-horse-battery-staple"),
            CTX,
        )
    except AuthenticationError as exc:
        disabled_error = str(exc)
    try:
        await service.login(LoginRequest(email="nobody@example.test", password="whatever12"), CTX)
    except AuthenticationError as exc:
        unknown_error = str(exc)
    assert disabled_error == unknown_error


# --- Rate limiting integration ----------------------------------------------


async def test_login_rate_limit_raises_after_threshold() -> None:
    service, repo = _make_service(login_rate_limit=2)
    user = make_user_record()
    repo.users[user.user_id] = user
    request = LoginRequest(email=user.email_normalized, password="wrong")

    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await service.login(request, CTX)
    with pytest.raises(RateLimitExceededError):
        await service.login(request, CTX)


async def test_refresh_rate_limit_raises_after_threshold() -> None:
    service, repo = _make_service(refresh_rate_limit=1)
    request = RefreshRequest(refresh_token="never-issued-token")

    with pytest.raises(SessionRevokedError):
        await service.refresh(request, CTX)
    with pytest.raises(RateLimitExceededError):
        await service.refresh(request, CTX)


async def test_refresh_rate_limit_applies_across_rotated_tokens_from_the_same_caller() -> None:
    """P5-REGRESSION-AUTH-001: a real client's refresh token changes on
    every successful call (single-use rotation). The rate limiter must
    still count these as the same caller -- keying on the token string
    itself let every rotated attempt escape the limit entirely."""
    service, repo = _make_service(refresh_rate_limit=1)
    user = make_user_record()
    repo.users[user.user_id] = user
    login_result = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), CTX
    )

    first = await service.refresh(RefreshRequest(refresh_token=login_result.refresh_token), CTX)
    assert first.refresh_token != login_result.refresh_token

    with pytest.raises(RateLimitExceededError):
        await service.refresh(RefreshRequest(refresh_token=first.refresh_token), CTX)


async def test_refresh_rate_limit_buckets_are_independent_per_caller() -> None:
    """A different `ip_marker` gets its own bucket -- one caller exceeding
    the limit must not deny a different caller's refresh attempts."""
    service, repo = _make_service(refresh_rate_limit=1)
    other_ctx = RequestContext(now=NOW, request_id="other-request-id", ip_marker="a" * 16)
    request = RefreshRequest(refresh_token="never-issued-token")

    with pytest.raises(SessionRevokedError):
        await service.refresh(request, CTX)
    with pytest.raises(RateLimitExceededError):
        await service.refresh(request, CTX)
    # A different caller (different ip_marker) is unaffected.
    with pytest.raises(SessionRevokedError):
        await service.refresh(request, other_ctx)


# --- Logout ------------------------------------------------------------------


async def test_service_logout_is_safe_to_call_with_an_unknown_token() -> None:
    service, _ = _make_service()
    await service.logout(LogoutRequest(refresh_token="never-issued"), CTX)


# --- /me -----------------------------------------------------------------


async def test_get_me_returns_public_user_and_active_memberships_only() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user
    case = make_case_record()
    repo.cases[case.case_id] = case
    active_membership = make_membership_record(
        case_id=case.case_id, user_id=user.user_id, role=CaseRole.ANALYST, is_active=True
    )
    inactive_membership = make_membership_record(user_id=user.user_id, is_active=False)
    repo.memberships[active_membership.membership_id] = active_membership
    repo.memberships[inactive_membership.membership_id] = inactive_membership

    me = await service.get_me(user.user_id)
    assert me.user.user_id == user.user_id
    assert len(me.case_memberships) == 1
    assert me.case_memberships[0].case_id == case.case_id
    assert me.case_memberships[0].role == CaseRole.ANALYST


async def test_get_me_rejects_inactive_user() -> None:
    service, repo = _make_service()
    user = make_user_record(is_active=False)
    repo.users[user.user_id] = user
    with pytest.raises(AuthenticationError):
        await service.get_me(user.user_id)


async def test_get_me_rejects_unknown_user() -> None:
    service, _ = _make_service()
    with pytest.raises(AuthenticationError):
        await service.get_me(uuid4())


# --- ADR-033: admin-forced password change ----------------------------------


async def test_provision_user_always_forces_a_password_change() -> None:
    service, repo = _make_service()
    request = ProvisionCaseHeadRequest(
        email="forced@example.test", password=DEFAULT_PASSWORD, display_name="A"
    )
    public_user = await service.provision_case_head(request, CTX, provisioned_by=uuid4())
    assert public_user.must_change_password is True
    assert repo.users[public_user.user_id].must_change_password is True


async def test_change_password_clears_the_forced_flag_and_accepts_the_new_password() -> None:
    service, repo = _make_service()
    user = make_user_record(must_change_password=True)
    repo.users[user.user_id] = user

    await service.change_password(
        user.user_id,
        ChangePasswordRequest(current_password=DEFAULT_PASSWORD, new_password="a-new-password-1"),
        CTX,
    )
    assert repo.users[user.user_id].must_change_password is False

    # The new password now works; the old one no longer does.
    result = await service.login(
        LoginRequest(email=user.email_normalized, password="a-new-password-1"), CTX
    )
    assert result.access_token
    with pytest.raises(AuthenticationError):
        await service.login(
            LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), CTX
        )


async def test_change_password_rejects_a_wrong_current_password() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user
    with pytest.raises(AuthenticationError):
        await service.change_password(
            user.user_id,
            ChangePasswordRequest(current_password="totally-wrong", new_password="a-new-pass-1"),
            CTX,
        )
    # Unaffected: no partial state change on a rejected attempt.
    assert repo.users[user.user_id].password_hash == user.password_hash


# --- ADR-033: TOTP enrollment ------------------------------------------------


async def test_enroll_then_confirm_mfa_enables_totp() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user

    enrollment = await service.enroll_mfa(user.user_id, CTX)
    assert enrollment.secret
    assert enrollment.provisioning_uri.startswith("otpauth://totp/")
    assert repo.users[user.user_id].totp_enabled is False  # not yet confirmed

    code = _current_totp_code(enrollment.secret)
    public_user = await service.confirm_mfa_enrollment(user.user_id, code, CTX)
    assert public_user.totp_enabled is True
    assert repo.users[user.user_id].totp_enabled is True


async def test_confirm_mfa_enrollment_rejects_a_wrong_code() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user
    enrollment = await service.enroll_mfa(user.user_id, CTX)
    with pytest.raises(AuthenticationError):
        await service.confirm_mfa_enrollment(user.user_id, "000000", CTX)
    assert repo.users[user.user_id].totp_enabled is False
    assert enrollment.secret  # sanity: enrollment itself succeeded


async def test_confirm_mfa_enrollment_without_a_pending_enrollment_is_rejected() -> None:
    service, repo = _make_service()
    user = make_user_record()
    repo.users[user.user_id] = user
    with pytest.raises(ValidationError):
        await service.confirm_mfa_enrollment(user.user_id, "123456", CTX)


# --- ADR-033: two-factor login -----------------------------------------------


async def test_login_with_totp_enabled_returns_a_challenge_not_tokens() -> None:
    service, repo = _make_service()
    user = make_user_record(totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
    repo.users[user.user_id] = user

    result = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), CTX
    )
    assert result.mfa_required is True
    assert result.mfa_token
    assert result.access_token is None
    assert result.refresh_token is None
    assert len(repo.sessions) == 0  # no session until the second factor is verified


async def test_verify_mfa_login_completes_the_session_with_a_correct_code() -> None:
    service, repo = _make_service()
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user_record(totp_secret=secret, totp_enabled=True)
    repo.users[user.user_id] = user

    challenge = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), _real_ctx()
    )
    code = _current_totp_code(secret)
    result = await service.verify_mfa_login(
        MfaLoginVerifyRequest(mfa_token=challenge.mfa_token, code=code), _real_ctx()
    )
    assert result.mfa_required is False
    assert result.access_token
    assert result.refresh_token
    assert len(repo.sessions) == 1


async def test_verify_mfa_login_rejects_a_wrong_code() -> None:
    service, repo = _make_service()
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user_record(totp_secret=secret, totp_enabled=True)
    repo.users[user.user_id] = user
    challenge = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), _real_ctx()
    )
    with pytest.raises(AuthenticationError):
        await service.verify_mfa_login(
            MfaLoginVerifyRequest(mfa_token=challenge.mfa_token, code="000000"), _real_ctx()
        )
    assert len(repo.sessions) == 0


async def test_verify_mfa_login_rejects_a_garbage_challenge_token() -> None:
    service, _ = _make_service()
    with pytest.raises(AuthenticationError):
        await service.verify_mfa_login(
            MfaLoginVerifyRequest(mfa_token="not-a-real-token", code="123456"), _real_ctx()
        )


async def test_verify_mfa_login_rate_limit_raises_after_threshold() -> None:
    service, repo = _make_service(mfa_rate_limit=2)
    secret = "JBSWY3DPEHPK3PXP"
    user = make_user_record(totp_secret=secret, totp_enabled=True)
    repo.users[user.user_id] = user
    challenge = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), _real_ctx()
    )
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await service.verify_mfa_login(
                MfaLoginVerifyRequest(mfa_token=challenge.mfa_token, code="000000"), _real_ctx()
            )
    with pytest.raises(RateLimitExceededError):
        await service.verify_mfa_login(
            MfaLoginVerifyRequest(mfa_token=challenge.mfa_token, code="000000"), _real_ctx()
        )


# --- ADR-033: admin-only lost-device/lost-password recovery -----------------


async def test_credential_reset_forces_change_and_clears_mfa() -> None:
    service, repo = _make_service()
    user = make_user_record(totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
    repo.users[user.user_id] = user
    login_result = await service.login(
        LoginRequest(email=user.email_normalized, password=DEFAULT_PASSWORD), _real_ctx()
    )
    code = _current_totp_code("JBSWY3DPEHPK3PXP")
    await service.verify_mfa_login(
        MfaLoginVerifyRequest(mfa_token=login_result.mfa_token, code=code), _real_ctx()
    )
    assert len(repo.sessions) == 1

    reset = await service.reset_credentials(user.user_id, CTX, reset_by=uuid4())
    assert reset.temporary_password
    updated = repo.users[user.user_id]
    assert updated.must_change_password is True
    assert updated.totp_enabled is False
    assert updated.totp_secret is None
    assert all(s.revoked_at is not None for s in repo.sessions.values())

    # The new temporary password actually works.
    fresh_login = await service.login(
        LoginRequest(email=user.email_normalized, password=reset.temporary_password), CTX
    )
    assert fresh_login.access_token


async def test_credential_reset_rejects_an_unknown_user() -> None:
    service, _ = _make_service()
    with pytest.raises(ValidationError):
        await service.reset_credentials(uuid4(), CTX, reset_by=uuid4())


def test_totp_test_fixture_secret_is_actually_valid_base32() -> None:
    """Guards the other tests here: `verify_totp` must accept our fixed test secret."""
    code = _current_totp_code("JBSWY3DPEHPK3PXP")
    assert verify_totp("JBSWY3DPEHPK3PXP", code)
