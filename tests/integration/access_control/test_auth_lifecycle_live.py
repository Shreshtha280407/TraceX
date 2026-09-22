"""Scenarios 2-4: register/login/refresh/logout against live PostgreSQL,
refresh-token hashing, and token-family revocation -- all against real persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.modules.access_control.errors import AuthenticationError, RefreshReuseDetectedError
from app.modules.access_control.models import (
    AdminProvisionUserRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
)
from app.modules.access_control.password import MIN_PASSWORD_LENGTH
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.access_control.repository import AccessControlRepository
from app.modules.access_control.service import AuthService, RequestContext
from app.modules.access_control.sessions import rotate_session
from app.modules.access_control.tokens import hash_refresh_token
from tests.fixtures.access_control.factories import make_user_record
from tests.integration.access_control.conftest import unique_email

VALID_PASSWORD = "correct-horse-battery-staple"
assert len(VALID_PASSWORD) >= MIN_PASSWORD_LENGTH


def _make_service(repository: AccessControlRepository) -> AuthService:
    return AuthService(
        repository=repository,
        login_rate_limiter=InMemoryRateLimiter(),
        refresh_rate_limiter=InMemoryRateLimiter(),
        jwt_secret="x" * 32,
        jwt_algorithm="HS256",
        jwt_issuer="tracex-api-test",
        jwt_audience="tracex-clients-test",
        access_token_ttl_seconds=900,
        refresh_token_ttl_seconds=1_209_600,
        login_rate_limit=1000,
        refresh_rate_limit=1000,
    )


def _ctx() -> RequestContext:
    return RequestContext(now=datetime.now(UTC), request_id="itest", ip_marker=None)


async def _seed_provisioner(
    repository: AccessControlRepository, cleanup_user_ids: list[UUID]
) -> UUID:
    """A real `provisioned_by` user_id -- `security_audit_events.
    user_id_nullable` has a real foreign key to `users.user_id` (see
    migration `7e8499f34f29`), so a fabricated `uuid4()` fails against
    live PostgreSQL with a genuine `ForeignKeyViolationError` (confirmed:
    this was a real, pre-existing bug in this test file, only ever caught
    once CI actually ran the integration suite against live infra --
    see docs/qa/known-limitations.md's "Gap-Closure re-close" section)."""
    provisioner = make_user_record()
    await repository.create_user(provisioner)
    cleanup_user_ids.append(provisioner.user_id)
    return provisioner.user_id


# --- Scenario 2 --------------------------------------------------------------


async def test_register_login_refresh_logout_round_trip_against_live_db(
    repository: AccessControlRepository, cleanup_user_ids: list[UUID]
) -> None:
    service = _make_service(repository)
    email = unique_email("live")
    provisioner_id = await _seed_provisioner(repository, cleanup_user_ids)

    public_user = await service.provision_user(
        AdminProvisionUserRequest(email=email, password=VALID_PASSWORD, display_name="Live Test"),
        _ctx(),
        provisioned_by=provisioner_id,
    )
    cleanup_user_ids.append(public_user.user_id)

    login_result = await service.login(LoginRequest(email=email, password=VALID_PASSWORD), _ctx())
    assert login_result.access_token
    assert login_result.refresh_token

    refresh_result = await service.refresh(
        RefreshRequest(refresh_token=login_result.refresh_token), _ctx()
    )
    assert refresh_result.refresh_token != login_result.refresh_token

    await service.logout(LogoutRequest(refresh_token=refresh_result.refresh_token), _ctx())

    stored_user = await repository.get_user_by_id(public_user.user_id)
    assert stored_user is not None
    assert stored_user.email_normalized == email


async def test_wrong_password_denied_against_live_db(
    repository: AccessControlRepository, cleanup_user_ids: list[UUID]
) -> None:
    service = _make_service(repository)
    email = unique_email("wrongpw")
    provisioner_id = await _seed_provisioner(repository, cleanup_user_ids)
    public_user = await service.provision_user(
        AdminProvisionUserRequest(email=email, password=VALID_PASSWORD, display_name="X"),
        _ctx(),
        provisioned_by=provisioner_id,
    )
    cleanup_user_ids.append(public_user.user_id)

    with pytest.raises(AuthenticationError):
        await service.login(LoginRequest(email=email, password="totally-wrong-pw"), _ctx())


# --- Scenario 3: refresh token stored hashed, never raw ---------------------


async def test_refresh_token_is_stored_hashed_not_raw(
    repository: AccessControlRepository, cleanup_user_ids: list[UUID]
) -> None:
    service = _make_service(repository)
    email = unique_email("hashcheck")
    provisioner_id = await _seed_provisioner(repository, cleanup_user_ids)

    public_user = await service.provision_user(
        AdminProvisionUserRequest(email=email, password=VALID_PASSWORD, display_name="Hash Check"),
        _ctx(),
        provisioned_by=provisioner_id,
    )
    cleanup_user_ids.append(public_user.user_id)
    login_result = await service.login(LoginRequest(email=email, password=VALID_PASSWORD), _ctx())

    session = await repository.get_session_by_refresh_token_hash(
        hash_refresh_token(login_result.refresh_token)
    )
    assert session is not None
    assert session.refresh_token_hash != login_result.refresh_token
    assert login_result.refresh_token not in session.refresh_token_hash
    assert len(session.refresh_token_hash) == 64  # sha256 hex digest


# --- Scenario 4: token-family revocation in live persistence ---------------


async def test_token_family_revocation_works_in_live_persistence(
    repository: AccessControlRepository, cleanup_user_ids: list[UUID]
) -> None:
    service = _make_service(repository)
    email = unique_email("family")
    provisioner_id = await _seed_provisioner(repository, cleanup_user_ids)

    public_user = await service.provision_user(
        AdminProvisionUserRequest(email=email, password=VALID_PASSWORD, display_name="Family Test"),
        _ctx(),
        provisioned_by=provisioner_id,
    )
    cleanup_user_ids.append(public_user.user_id)
    login_result = await service.login(LoginRequest(email=email, password=VALID_PASSWORD), _ctx())

    rotated = await rotate_session(
        repository,
        refresh_token=login_result.refresh_token,
        now=datetime.now(UTC),
        ttl_seconds=1_209_600,
    )

    with pytest.raises(RefreshReuseDetectedError):
        await rotate_session(
            repository,
            refresh_token=login_result.refresh_token,  # replay of the rotated-away token
            now=datetime.now(UTC),
            ttl_seconds=1_209_600,
        )

    # The family's current, otherwise-legitimate leaf session is also revoked.
    leaf_session = await repository.get_session_by_id(rotated.session.session_id)
    assert leaf_session is not None
    assert leaf_session.revoked_at is not None
