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
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.access_control.service import AuthService, RequestContext
from tests.fixtures.access_control.factories import (
    DEFAULT_PASSWORD,
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
CTX = RequestContext(now=NOW, request_id="test-request-id", ip_marker=None)


def _make_service(
    repository: FakeAccessControlRepository | None = None,
    *,
    login_rate_limit: int = 1000,
    refresh_rate_limit: int = 1000,
) -> tuple[AuthService, FakeAccessControlRepository]:
    repo = repository or FakeAccessControlRepository()
    service = AuthService(
        repository=repo,  # type: ignore[arg-type]
        login_rate_limiter=InMemoryRateLimiter(),
        refresh_rate_limiter=InMemoryRateLimiter(),
        jwt_secret="x" * 32,
        jwt_algorithm="HS256",
        jwt_issuer="tracex-api-test",
        jwt_audience="tracex-clients-test",
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=1_209_600,
        login_rate_limit=login_rate_limit,
        refresh_rate_limit=refresh_rate_limit,
    )
    return service, repo


# --- Scenario 2: registration rejects invalid data safely -------------------


def test_register_request_rejects_short_password() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic.ValidationError, not this module's
        RegisterRequest(email="a@b.com", password="short", display_name="A")


def test_register_request_rejects_malformed_email() -> None:
    with pytest.raises(Exception):  # noqa: B017
        RegisterRequest(email="not-an-email", password=DEFAULT_PASSWORD, display_name="A")


def test_register_request_normalizes_email() -> None:
    request = RegisterRequest(
        email="  Someone@Example.COM  ", password=DEFAULT_PASSWORD, display_name="A"
    )
    assert request.email == "someone@example.com"


async def test_register_creates_a_user_and_never_returns_the_password_hash() -> None:
    service, repo = _make_service()
    request = RegisterRequest(
        email="new@example.test", password=DEFAULT_PASSWORD, display_name="New"
    )
    public_user = await service.register(request, CTX)

    assert public_user.email_normalized == "new@example.test"
    assert not hasattr(public_user, "password_hash")
    assert "password_hash" not in public_user.model_dump()
    stored = repo.users[public_user.user_id]
    assert stored.password_hash != DEFAULT_PASSWORD


async def test_register_rejects_duplicate_email() -> None:
    service, repo = _make_service()
    request = RegisterRequest(email="dup@example.test", password=DEFAULT_PASSWORD, display_name="A")
    await service.register(request, CTX)
    with pytest.raises(ValidationError):
        await service.register(request, CTX)


async def test_registered_user_is_never_automatically_privileged() -> None:
    # There is no "role"/"is_admin" field on PublicUser or UserRecord at
    # all -- registration cannot grant case-level privilege, since that
    # only ever comes from an explicit `case_memberships` row.
    service, repo = _make_service()
    request = RegisterRequest(
        email="plain@example.test", password=DEFAULT_PASSWORD, display_name="A"
    )
    public_user = await service.register(request, CTX)
    assert "role" not in public_user.model_dump()
    assert repo.memberships == {}


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
