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
}

for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

from app.main import app  # noqa: E402  (must follow env setup above)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
