"""Scenario 11: no password, access token, refresh token, secret, or password hash
anywhere in API responses, audit events, or logs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_mfa_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import SystemRole
from app.modules.access_control.password import hash_password
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from tests.fixtures.access_control.factories import make_user_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

SECRET_PASSWORD = "super-secret-password-value-123"  # noqa: S105 - test fixture, not a real credential
_FAKE_DB_SECRET = "hunter2-super-secret-db-password"  # noqa: S105


@pytest.fixture
def fake_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def _override_dependencies(fake_repository: FakeAccessControlRepository) -> Iterator[None]:
    login_limiter = InMemoryRateLimiter()
    refresh_limiter = InMemoryRateLimiter()
    mfa_limiter = InMemoryRateLimiter()
    app.dependency_overrides[get_access_control_repository] = lambda: fake_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: login_limiter
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: refresh_limiter
    app.dependency_overrides[get_mfa_rate_limiter] = lambda: mfa_limiter
    yield
    app.dependency_overrides.pop(get_access_control_repository, None)
    app.dependency_overrides.pop(get_login_rate_limiter, None)
    app.dependency_overrides.pop(get_refresh_rate_limiter, None)
    app.dependency_overrides.pop(get_mfa_rate_limiter, None)


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    # See `tests/conftest.py::client`'s comment: without
    # `raise_app_exceptions=False`, httpx re-raises an unhandled exception
    # to the test instead of returning the safe response the app actually
    # sent, which does not match real client/server behavior.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_case_head_provision_response_and_logs_never_contain_the_password(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No public self-registration exists (G5) -- this now exercises the
    Provisioner-only Case Head creation path.
    """
    caplog.set_level(logging.DEBUG)
    provisioner_email = "provisioner-secrettest@example.test"
    await fake_repository.create_user(
        make_user_record(email_normalized=provisioner_email, system_role=SystemRole.PROVISIONER)
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": provisioner_email, "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    provisioner_token = login.json()["access_token"]

    response = await client.post(
        "/api/v1/provisioning/case-heads",
        headers={"Authorization": f"Bearer {provisioner_token}"},
        json={
            "email": "secrettest@example.test",
            "password": SECRET_PASSWORD,
            "display_name": "X",
        },
    )
    assert response.status_code == 201
    assert SECRET_PASSWORD not in response.text
    assert "password_hash" not in response.text
    assert "password" not in response.json()
    assert SECRET_PASSWORD not in caplog.text


async def test_login_success_never_leaks_password_hash_or_the_other_token(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    email = "loginsecret@example.test"
    await fake_repository.create_user(
        make_user_record(email_normalized=email, password_hash=hash_password(SECRET_PASSWORD))
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": SECRET_PASSWORD}
    )
    assert response.status_code == 200
    tokens = response.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    assert SECRET_PASSWORD not in response.text

    stored_user = next(iter(fake_repository.users.values()))
    assert stored_user.password_hash not in response.text

    for session in fake_repository.sessions.values():
        assert refresh_token not in session.refresh_token_hash
        assert session.refresh_token_hash not in response.text

    for event in fake_repository.audit_events:
        blob = event.model_dump_json()
        assert SECRET_PASSWORD not in blob
        assert access_token not in blob
        assert refresh_token not in blob
        assert stored_user.password_hash not in blob

    assert SECRET_PASSWORD not in caplog.text
    assert access_token not in caplog.text
    assert refresh_token not in caplog.text


async def test_login_failure_never_leaks_the_attempted_password(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.test", "password": SECRET_PASSWORD},
    )
    assert response.status_code == 401
    assert SECRET_PASSWORD not in response.text
    assert SECRET_PASSWORD not in caplog.text
    for event in fake_repository.audit_events:
        assert SECRET_PASSWORD not in event.model_dump_json()


async def test_refresh_and_logout_never_leak_the_refresh_token_value(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    email = "refreshsecret@example.test"
    await fake_repository.create_user(
        make_user_record(email_normalized=email, password_hash=hash_password(SECRET_PASSWORD))
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": SECRET_PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]

    refresh = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_token not in refresh.text

    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": refresh.json()["refresh_token"]}
    )
    assert logout.status_code == 204

    for event in fake_repository.audit_events:
        assert refresh_token not in event.model_dump_json()
    assert refresh_token not in caplog.text


async def test_totp_secret_never_appears_outside_the_enroll_response(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`/mfa/enroll` legitimately returns the secret once (that's its job) --
    it must never additionally appear in `/me`, `/mfa/verify`, a login
    response, or logs."""
    caplog.set_level(logging.DEBUG)
    email = "totpsecret@example.test"
    await fake_repository.create_user(
        make_user_record(email_normalized=email, password_hash=hash_password(SECRET_PASSWORD))
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": SECRET_PASSWORD}
    )
    token = login.json()["access_token"]

    enroll = await client.post(
        "/api/v1/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"}
    )
    secret = enroll.json()["secret"]
    assert secret in enroll.text  # the one legitimate place it appears

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert secret not in me.text
    assert "totp_secret" not in me.text

    for event in fake_repository.audit_events:
        assert secret not in event.model_dump_json()
    assert secret not in caplog.text

    stored = next(iter(fake_repository.users.values()))
    assert stored.totp_secret == secret  # sanity: it really was persisted


async def test_unexpected_backend_failure_never_leaks_a_connection_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors `tests/security/test_no_secret_leakage.py`'s existing pattern for `/readyz`."""
    caplog.set_level(logging.DEBUG)

    class _LeakyRepository:
        async def get_user_by_email(self, email_normalized: str) -> None:
            raise ConnectionError(f"postgresql://tracex:{_FAKE_DB_SECRET}@db:5432/tracex")

    app.dependency_overrides[get_access_control_repository] = lambda: _LeakyRepository()
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    try:
        # `raise_app_exceptions=False`: this test deliberately drives a
        # genuinely unhandled exception (no local per-endpoint catch
        # remains -- see `app/modules/access_control/api.py`) through the
        # *central* `unhandled_exception_handler`. The default transport
        # setting would re-raise it to this test instead of returning the
        # safe response the app actually sent -- see
        # `tests/conftest.py::client`'s comment for the full explanation.
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "whoever@example.test", "password": "irrelevant1"},
            )
    finally:
        app.dependency_overrides.pop(get_access_control_repository, None)
        app.dependency_overrides.pop(get_login_rate_limiter, None)
        app.dependency_overrides.pop(get_refresh_rate_limiter, None)

    assert response.status_code == 500
    assert _FAKE_DB_SECRET not in response.text
    assert "postgresql://" not in response.text
    assert "ConnectionError" not in response.text
    assert "Traceback" not in response.text
    assert _FAKE_DB_SECRET not in caplog.text
