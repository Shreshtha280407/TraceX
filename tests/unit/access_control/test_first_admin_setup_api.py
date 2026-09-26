"""Private first-admin setup: one-time, rate-limited, and secret-safe."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import AppEnv, Settings, get_settings
from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_first_admin_setup_rate_limiter,
)
from app.modules.access_control.models import SystemRole
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from tests.fixtures.access_control.factories import make_user_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

SETUP_TOKEN = "test-only-first-admin-setup-token-which-is-long-enough"


@pytest.fixture
def repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        first_admin_setup_enabled=True,
        first_admin_setup_token=SETUP_TOKEN,
        first_admin_setup_rate_limit=5,
    )


@pytest.fixture
def overrides(repository: FakeAccessControlRepository, settings: Settings) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: repository
    app.dependency_overrides[get_first_admin_setup_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_settings] = lambda: settings
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(overrides: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _payload() -> dict[str, str]:
    return {
        "organization_name": "Private Investigations Unit",
        "display_name": "Initial Administrator",
        "email": "first-admin@example.test",
        "password": "safe-first-admin-password",
    }


def test_production_rejects_enabled_setup_without_a_strong_token() -> None:
    with pytest.raises(ValidationError, match="first_admin_setup_token"):
        Settings(
            app_env=AppEnv.PRODUCTION,
            first_admin_setup_enabled=True,
            first_admin_setup_token="too-short",
        )


async def test_status_is_required_only_before_an_active_admin_exists(
    client: AsyncClient, repository: FakeAccessControlRepository
) -> None:
    before = await client.get("/api/v1/setup/first-admin/status")
    assert before.status_code == 200
    assert before.json() == {"setup_required": True}

    await repository.create_user(make_user_record(system_role=SystemRole.ADMIN))
    after = await client.get("/api/v1/setup/first-admin/status")
    assert after.status_code == 200
    assert after.json() == {"setup_required": False}


async def test_setup_requires_header_token_and_never_echoes_secrets(client: AsyncClient) -> None:
    missing = await client.post("/api/v1/setup/first-admin", json=_payload())
    invalid = await client.post(
        "/api/v1/setup/first-admin",
        json=_payload(),
        headers={"X-TraceX-First-Admin-Setup-Token": "incorrect-token"},
    )
    for response in (missing, invalid):
        assert response.status_code == 403
        assert SETUP_TOKEN not in response.text
        assert _payload()["password"] not in response.text
        assert response.headers["cache-control"] == "no-store"


async def test_first_admin_creation_closes_setup_and_audits_without_secrets(
    client: AsyncClient, repository: FakeAccessControlRepository
) -> None:
    response = await client.post(
        "/api/v1/setup/first-admin",
        json=_payload(),
        headers={"X-TraceX-First-Admin-Setup-Token": SETUP_TOKEN},
    )
    assert response.status_code == 201, response.text
    assert response.json()["system_role"] == "admin"
    assert "password" not in response.json()
    assert "totp_secret" not in response.json()
    assert len(repository.audit_events) == 1
    audit = repository.audit_events[0]
    assert audit.event_type == "admin.first_setup"
    assert SETUP_TOKEN not in str(audit.metadata_safe_json)
    assert _payload()["password"] not in str(audit.metadata_safe_json)

    second = await client.post(
        "/api/v1/setup/first-admin",
        json={**_payload(), "email": "another@example.test"},
        headers={"X-TraceX-First-Admin-Setup-Token": SETUP_TOKEN},
    )
    assert second.status_code == 409


async def test_concurrent_first_admin_requests_allow_exactly_one(
    client: AsyncClient, repository: FakeAccessControlRepository
) -> None:
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/setup/first-admin",
                json={**_payload(), "email": f"first-{index}@example.test"},
                headers={"X-TraceX-First-Admin-Setup-Token": SETUP_TOKEN},
            )
            for index in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert await repository.count_users_with_system_role("admin") == 1
