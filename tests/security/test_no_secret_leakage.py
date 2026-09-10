"""No secret, connection string, or stack trace ever reaches an API response.

Complements the safe-body assertions in tests/unit/test_api_health.py with
handler-level checks that don't depend on a specific route wiring.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from starlette.requests import Request

from app.core.errors import unhandled_exception_handler
from app.dependencies.services import HealthCheckMap, get_health_checks
from app.main import app

_FAKE_SECRET = "db_password=hunter2-super-secret"  # noqa: S105  (test fixture, not a real credential)


async def test_unhandled_exception_handler_hides_internal_detail() -> None:
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
    request = Request(scope)
    exc = RuntimeError(f"connection failed: {_FAKE_SECRET}")

    response = await unhandled_exception_handler(request, exc)

    body = response.body.decode()
    assert _FAKE_SECRET not in body
    assert "RuntimeError" not in body
    assert response.status_code == 500


@pytest.fixture
def leaking_dependency() -> Iterator[None]:
    async def _ok() -> None:
        return None

    async def _leaky_failure() -> None:
        raise ConnectionError(f"postgresql://tracex:{_FAKE_SECRET}@db:5432/tracex")

    def _override() -> HealthCheckMap:
        return {"postgres": _leaky_failure, "neo4j": _ok, "redis": _ok, "minio": _ok}

    app.dependency_overrides[get_health_checks] = _override
    yield
    app.dependency_overrides.pop(get_health_checks, None)


async def test_readyz_never_echoes_dependency_exception_text(
    client: AsyncClient, leaking_dependency: None
) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert _FAKE_SECRET not in response.text
    assert "postgresql://" not in response.text
