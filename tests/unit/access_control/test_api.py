"""HTTP-layer tests: endpoint wiring, rate-limit 429s, security headers, Cache-Control.

Overrides `get_access_control_repository`/`get_login_rate_limiter`/
`get_refresh_rate_limiter` with an in-memory fake and deterministic
limiters, so the whole `/api/v1/auth` router is exercised over real ASGI
HTTP semantics without any live PostgreSQL/Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.password import MIN_PASSWORD_LENGTH
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

VALID_PASSWORD = "correct-horse-battery-staple"
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
    app.dependency_overrides[get_access_control_repository] = lambda: fake_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: login_limiter
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: refresh_limiter
    yield
    app.dependency_overrides.pop(get_access_control_repository, None)
    app.dependency_overrides.pop(get_login_rate_limiter, None)
    app.dependency_overrides.pop(get_refresh_rate_limiter, None)


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    # See the shared `tests/conftest.py::client` fixture's comment: without
    # `raise_app_exceptions=False`, httpx re-raises an unhandled exception
    # to the test instead of returning the safe response the app actually
    # sent, which does not match real client/server behavior.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _register(client: AsyncClient, email: str) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": VALID_PASSWORD, "display_name": "Test User"},
    )
    assert response.status_code == 201, response.text


# --- End-to-end wiring smoke ------------------------------------------------


async def test_full_register_login_refresh_logout_round_trip(client: AsyncClient) -> None:
    email = "roundtrip@example.test"
    await _register(client, email)

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


async def test_register_duplicate_email_is_conflict(client: AsyncClient) -> None:
    await _register(client, "dup-http@example.test")
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "dup-http@example.test",
            "password": VALID_PASSWORD,
            "display_name": "Again",
        },
    )
    assert response.status_code == 409


async def test_login_wrong_password_is_generic_401(client: AsyncClient) -> None:
    await _register(client, "wrongpw@example.test")
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


async def test_login_rate_limit_returns_429(client: AsyncClient) -> None:
    await _register(client, "ratelimited@example.test")
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


async def test_cache_control_no_store_on_successful_login(client: AsyncClient) -> None:
    await _register(client, "cachecontrol@example.test")
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
