"""HTTP-layer tests: endpoint wiring, rate-limit 429s, security headers, Cache-Control.

Overrides `get_access_control_repository`/`get_login_rate_limiter`/
`get_refresh_rate_limiter` with an in-memory fake and deterministic
limiters, so the whole `/api/v1/auth` router is exercised over real ASGI
HTTP semantics without any live PostgreSQL/Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import uuid4

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
from app.modules.access_control.models import (
    SystemRole,
    WorkerCredentialRecord,
    WorkerCredentialStatus,
)
from app.modules.access_control.password import MIN_PASSWORD_LENGTH
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from tests.fixtures.access_control.factories import DEFAULT_PASSWORD, make_user_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

VALID_PASSWORD = DEFAULT_PASSWORD
assert len(VALID_PASSWORD) >= MIN_PASSWORD_LENGTH


@pytest.fixture
def fake_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def _override_dependencies(fake_repository: FakeAccessControlRepository) -> Iterator[None]:
    # Construct each limiter ONCE and close over that instance -- FastAPI
    # calls the override callable fresh on every dependency resolution, so
    # `lambda: InMemoryRateLimiter()` would silently reset the count on
    # every single request and rate limiting would never trigger.
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
    # See the shared `tests/conftest.py::client` fixture's comment: without
    # `raise_app_exceptions=False`, httpx re-raises an unhandled exception
    # to the test instead of returning the safe response the app actually
    # sent, which does not match real client/server behavior.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _register(
    client: AsyncClient, email: str, fake_repository: FakeAccessControlRepository
) -> None:
    """Seeds an active user directly (no public self-registration exists -- G5)."""
    await fake_repository.create_user(make_user_record(email_normalized=email))


async def _provision_via_admin_route(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    *,
    email: str,
    system_role: SystemRole | None = None,
) -> tuple[str, dict]:
    """Seed an admin, log them in, then provision `email` through the real HTTP route."""
    admin_email = f"admin-{uuid4().hex[:10]}@example.test"
    admin = make_user_record(email_normalized=admin_email, system_role=SystemRole.ADMIN)
    await fake_repository.create_user(admin)
    login = await client.post(
        "/api/v1/auth/login", json={"email": admin_email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    admin_token = login.json()["access_token"]
    response = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "email": email,
            "password": VALID_PASSWORD,
            "display_name": "Provisioned User",
            "system_role": system_role.value if system_role else None,
        },
    )
    return admin_token, response.json() if response.status_code == 201 else {
        "status": response.status_code,
        "body": response.text,
    }


# --- End-to-end wiring smoke ------------------------------------------------


async def test_full_register_login_refresh_logout_round_trip(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    email = "roundtrip@example.test"
    await _register(client, email, fake_repository)

    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200
    tokens = login.json()
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["user"]["email_normalized"] == email

    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh.status_code == 200
    new_tokens = refresh.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # The old refresh token is now invalid (rotated away).
    reuse = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert reuse.status_code == 401

    logout = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert logout.status_code == 204

    # Idempotent: logging out again with the same (now-revoked) token is not an error.
    logout_again = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert logout_again.status_code == 204


async def test_public_register_route_no_longer_exists(client: AsyncClient) -> None:
    """G5: no public self-registration surface -- never a 201, ever."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "nobody@example.test", "password": VALID_PASSWORD, "display_name": "X"},
    )
    assert response.status_code == 404


async def test_admin_provision_duplicate_email_is_conflict(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    admin_token, first = await _provision_via_admin_route(
        client, fake_repository, email="dup-admin@example.test"
    )
    assert "user_id" in first, first
    response = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "email": "dup-admin@example.test",
            "password": VALID_PASSWORD,
            "display_name": "Again",
        },
    )
    assert response.status_code == 409


async def test_admin_provision_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/admin/users",
        json={"email": "nobody@example.test", "password": VALID_PASSWORD, "display_name": "X"},
    )
    assert response.status_code == 401


async def test_admin_provision_rejects_non_admin_caller(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "plain-user@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "plain-user@example.test", "password": VALID_PASSWORD}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    response = await client.post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "escalated@example.test", "password": VALID_PASSWORD, "display_name": "X"},
    )
    assert response.status_code == 403


async def test_login_wrong_password_is_generic_401(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "wrongpw@example.test", fake_repository)
    response = await client.post(
        "/api/v1/auth/login", json={"email": "wrongpw@example.test", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "invalid email or password"


async def test_me_without_authorization_header_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_me_with_garbage_bearer_token_is_401(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


# --- Scenario 10: rate limiting returns safe 429 ----------------------------


async def test_login_rate_limit_returns_429(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "ratelimited@example.test", fake_repository)
    body = {"email": "ratelimited@example.test", "password": "wrong-password"}

    from app.core.config import get_settings

    limit = get_settings().auth_login_rate_limit
    statuses = []
    for _ in range(limit + 2):
        response = await client.post("/api/v1/auth/login", json=body)
        statuses.append(response.status_code)

    assert statuses[:limit] == [401] * limit
    assert 429 in statuses[limit:]
    final = await client.post("/api/v1/auth/login", json=body)
    assert final.status_code == 429
    assert "password" not in final.text
    assert "wrong-password" not in final.text


async def test_refresh_rate_limit_returns_429(client: AsyncClient) -> None:
    from app.core.config import get_settings

    limit = get_settings().auth_refresh_rate_limit
    body = {"refresh_token": "never-issued-token"}
    statuses = []
    for _ in range(limit + 2):
        response = await client.post("/api/v1/auth/refresh", json=body)
        statuses.append(response.status_code)

    assert statuses[:limit] == [401] * limit
    assert 429 in statuses[limit:]


async def test_refresh_rate_limit_returns_429_for_a_real_rotating_authenticated_client(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """P5-REGRESSION-AUTH-001 regression test: a legitimate client presents
    its newest rotated refresh token on every call, exactly as a real
    client must (the previous token is now invalid -- reuse would 401).
    Rate limiting must still trigger once this same client exceeds the
    configured attempt count, even though every individual token string
    differs from the last -- keying the limiter on the token itself made
    this unreachable (every attempt landed in its own one-shot bucket).

    Uses a fixed-clock limiter (not the fixture's real-time default): this
    test does real bcrypt/JWT work per call, and the fixed rate-limit
    window is real wall-clock time -- under full-suite load, `limit + 2`
    real requests could otherwise straddle a window boundary and reset
    the count, making the test flaky rather than actually verifying the
    limiter's identity-keying behavior.
    """
    from app.core.config import get_settings

    # Construct the limiter ONCE and close over that instance -- FastAPI
    # calls the override callable fresh on every dependency resolution
    # (see `_override_dependencies` above), so a fresh instance per call
    # would silently reset the count on every request.
    fixed_clock_limiter = InMemoryRateLimiter(clock=lambda: 0.0)
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: fixed_clock_limiter
    limit = get_settings().auth_refresh_rate_limit
    email = "rotating-refresh@example.test"
    await _register(client, email, fake_repository)
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]

    statuses = []
    for _ in range(limit + 2):
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        statuses.append(response.status_code)
        if response.status_code == 200:
            refresh_token = response.json()["refresh_token"]

    assert statuses[:limit] == [200] * limit
    assert 429 in statuses[limit:]


# --- Scenario 18: security headers ------------------------------------------


async def test_security_headers_present_on_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_security_headers_present_on_auth_endpoint(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "irrelevant1"}
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_no_permissive_cors_header_is_ever_set(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


# --- Scenario 19: Cache-Control: no-store on auth responses -----------------


async def test_cache_control_no_store_on_successful_login(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "cachecontrol@example.test", fake_repository)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "cachecontrol@example.test", "password": VALID_PASSWORD},
    )
    assert response.headers["cache-control"] == "no-store"


async def test_cache_control_no_store_on_failed_login(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "irrelevant1"}
    )
    assert response.headers["cache-control"] == "no-store"


async def test_cache_control_no_store_on_me(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.headers["cache-control"] == "no-store"


async def test_cache_control_not_forced_on_unrelated_endpoints(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers.get("cache-control") != "no-store"


# --- Gap-Closure WP-6 (G16): GET /api/v1/admin/workers -----------------------


async def _login_as_admin(client: AsyncClient, fake_repository: FakeAccessControlRepository) -> str:
    admin_email = f"admin-{uuid4().hex[:10]}@example.test"
    await fake_repository.create_user(
        make_user_record(email_normalized=admin_email, system_role=SystemRole.ADMIN)
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": admin_email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def test_list_workers_requires_admin(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "not-admin@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "not-admin@example.test", "password": VALID_PASSWORD},
    )
    token = login.json()["access_token"]

    response = await client.get(
        "/api/v1/admin/workers", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


async def test_list_workers_unauthenticated_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/workers")
    assert response.status_code == 401


async def _current_totp_code(secret: str) -> str:
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


# --- ADR-033: forced first-login password change ----------------------------


async def test_admin_provisioned_user_must_change_password(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    _admin_token, created = await _provision_via_admin_route(
        client, fake_repository, email="mustchange@example.test"
    )
    assert created["must_change_password"] is True

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "mustchange@example.test", "password": VALID_PASSWORD},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["user"]["must_change_password"] is True

    change = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": VALID_PASSWORD, "new_password": "brand-new-password-1"},
    )
    assert change.status_code == 204

    me_after = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_after.json()["user"]["must_change_password"] is False


async def test_change_password_wrong_current_password_is_401(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "changepw@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "changepw@example.test", "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]
    response = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "wrong-one", "new_password": "brand-new-password-1"},
    )
    assert response.status_code == 401


# --- ADR-033: TOTP enrollment + two-factor login over real HTTP -------------


async def test_full_mfa_enrollment_and_two_factor_login_round_trip(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "mfauser@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "mfauser@example.test", "password": VALID_PASSWORD}
    )
    assert login.json()["mfa_required"] is False
    token = login.json()["access_token"]

    enroll = await client.post(
        "/api/v1/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"}
    )
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    assert enroll.json()["provisioning_uri"].startswith("otpauth://totp/")

    code = await _current_totp_code(secret)
    verify = await client.post(
        "/api/v1/auth/mfa/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": code},
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["totp_enabled"] is True

    # Every login from now on requires the second factor.
    second_login = await client.post(
        "/api/v1/auth/login", json={"email": "mfauser@example.test", "password": VALID_PASSWORD}
    )
    assert second_login.status_code == 200
    body = second_login.json()
    assert body["mfa_required"] is True
    assert body["access_token"] is None
    mfa_token = body["mfa_token"]

    login_code = await _current_totp_code(secret)
    finish = await client.post(
        "/api/v1/auth/mfa/login-verify", json={"mfa_token": mfa_token, "code": login_code}
    )
    assert finish.status_code == 200, finish.text
    finished = finish.json()
    assert finished["mfa_required"] is False
    assert finished["access_token"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {finished['access_token']}"}
    )
    assert me.json()["user"]["totp_enabled"] is True


async def test_mfa_login_verify_wrong_code_is_401(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "mfawrong@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "mfawrong@example.test", "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]
    enroll = await client.post(
        "/api/v1/auth/mfa/enroll", headers={"Authorization": f"Bearer {token}"}
    )
    secret = enroll.json()["secret"]
    code = await _current_totp_code(secret)
    await client.post(
        "/api/v1/auth/mfa/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": code},
    )

    second_login = await client.post(
        "/api/v1/auth/login", json={"email": "mfawrong@example.test", "password": VALID_PASSWORD}
    )
    mfa_token = second_login.json()["mfa_token"]
    response = await client.post(
        "/api/v1/auth/mfa/login-verify", json={"mfa_token": mfa_token, "code": "000000"}
    )
    assert response.status_code == 401


async def test_mfa_enroll_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/mfa/enroll")
    assert response.status_code == 401


# --- ADR-033: admin-only lost-device/lost-password recovery -----------------


async def test_admin_reset_credentials_round_trip(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    admin_token, created = await _provision_via_admin_route(
        client, fake_repository, email="resetme@example.test"
    )
    user_id = created["user_id"]

    reset = await client.post(
        f"/api/v1/admin/users/{user_id}/reset-credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert reset.status_code == 200, reset.text
    new_password = reset.json()["temporary_password"]
    assert new_password

    login = await client.post(
        "/api/v1/auth/login", json={"email": "resetme@example.test", "password": new_password}
    )
    assert login.status_code == 200
    assert login.json()["access_token"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert me.json()["user"]["must_change_password"] is True
    assert me.json()["user"]["totp_enabled"] is False


async def test_admin_reset_credentials_requires_admin(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "notadmin@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "notadmin@example.test", "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]
    response = await client.post(
        f"/api/v1/admin/users/{uuid4()}/reset-credentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_admin_reset_credentials_unknown_user_is_404(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    admin_token = await _login_as_admin(client, fake_repository)
    response = await client.post(
        f"/api/v1/admin/users/{uuid4()}/reset-credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 404


# --- Admin-only account listing (backs the Settings/Security admin picker) --


async def test_list_users_requires_admin(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    await _register(client, "notadmin-listing@example.test", fake_repository)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "notadmin-listing@example.test", "password": VALID_PASSWORD},
    )
    token = login.json()["access_token"]
    response = await client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


async def test_list_users_unauthenticated_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/users")
    assert response.status_code == 401


async def test_list_users_returns_real_accounts_never_a_secret(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    admin_token, created = await _provision_via_admin_route(
        client, fake_repository, email="listed-user@example.test"
    )
    response = await client.get(
        "/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200, response.text
    emails = {item["email_normalized"] for item in response.json()["items"]}
    assert "listed-user@example.test" in emails
    assert created["user_id"] in {item["user_id"] for item in response.json()["items"]}
    assert "password_hash" not in response.text
    assert "totp_secret" not in response.text
    for item in response.json()["items"]:
        assert set(item.keys()) == {
            "user_id",
            "email_normalized",
            "display_name",
            "is_active",
            "created_at",
            "system_role",
            "must_change_password",
            "totp_enabled",
        }


async def test_list_users_respects_limit_and_offset(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    admin_token = await _login_as_admin(client, fake_repository)
    for i in range(3):
        await fake_repository.create_user(
            make_user_record(email_normalized=f"page-{i}@example.test")
        )

    first_page = await client.get(
        "/api/v1/admin/users?limit=2&offset=0", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert first_page.status_code == 200
    assert len(first_page.json()["items"]) == 2

    second_page = await client.get(
        "/api/v1/admin/users?limit=2&offset=2", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert second_page.status_code == 200
    first_ids = {item["user_id"] for item in first_page.json()["items"]}
    second_ids = {item["user_id"] for item in second_page.json()["items"]}
    assert first_ids.isdisjoint(second_ids)


async def test_list_workers_reports_liveness_and_never_the_credential_digest(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    admin_token = await _login_as_admin(client, fake_repository)
    worker_id = uuid4()
    await fake_repository.create_worker_credential(
        WorkerCredentialRecord(
            worker_id=worker_id,
            display_name="cdr-worker-1",
            status=WorkerCredentialStatus.ACTIVE,
            allowed_processor_names=("cdr_generic_v1",),
            credential_digest="super-secret-digest-must-never-appear-in-response",
            created_at=datetime.now(UTC),
            rotated_at=None,
            revoked_at=None,
            last_seen_at=datetime.now(UTC),
        )
    )

    response = await client.get(
        "/api/v1/admin/workers", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["worker_id"] == str(worker_id)
    assert item["liveness"] == "active"
    assert "credential_digest" not in response.text
    assert "super-secret-digest" not in response.text
