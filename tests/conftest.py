"""Shared test setup.

The environment defaults below are set at *module import time* (not inside
a fixture), so they are in place before any test module's own `import
app.main` executes during collection. Every value is a syntactically-valid
placeholder only -- nothing in the default test suite opens a real
connection to these hosts (see tests/integration for tests that do, and
skip themselves when the real service isn't reachable).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_TEST_ENV_DEFAULTS = {
    "APP_NAME": "tracex-api-test",
    "APP_ENV": "test",
    "APP_HOST": "0.0.0.0",
    "APP_PORT": "8000",
    "LOG_LEVEL": "INFO",
    "POSTGRES_DSN": "postgresql+asyncpg://tracex:test-only@localhost:5432/tracex_test",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USERNAME": "neo4j",
    "NEO4J_PASSWORD": "test-only",
    "REDIS_URL": "redis://localhost:6379/0",
    "MINIO_ENDPOINT": "localhost:9000",
    "MINIO_ACCESS_KEY": "tracex-minio-test",
    "MINIO_SECRET_KEY": "test-only",
    "MINIO_SECURE": "false",
    "MINIO_BUCKET": "tracex-evidence-test",
    "CONTRACT_VERSION": "v1",
    "AUTH_JWT_SECRET": "test-only-jwt-secret-32-characters-min",
    "AUTH_JWT_ALGORITHM": "HS256",
    "AUTH_JWT_ISSUER": "tracex-api-test",
    "AUTH_JWT_AUDIENCE": "tracex-clients-test",
    "AUTH_ACCESS_TOKEN_TTL_SECONDS": "900",
    "AUTH_REFRESH_TOKEN_TTL_SECONDS": "1209600",
    "AUTH_LOGIN_RATE_LIMIT": "5",
    "AUTH_REFRESH_RATE_LIMIT": "20",
}

for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

from app.main import app  # noqa: E402  (must follow env setup above)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
