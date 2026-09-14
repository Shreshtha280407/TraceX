"""Shared test setup.

The environment defaults below are set at *module import time* (not inside
a fixture), so they are in place before any test module's own `import
app.main` executes during collection. Every value is a syntactically-valid
placeholder only -- nothing in the default test suite opens a real
connection to these hosts (see tests/integration for tests that do, and
skip themselves when the real service isn't reachable).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import anyio.to_thread
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
    "MAX_EVIDENCE_BYTES": "209715200",
    "WORKER_LEASE_SECONDS": "300",
    # A fixed, non-secret test-only pepper: real worker-credential digest
    # tests need `hash_worker_credential` to behave deterministically and
    # identically between whatever created a test credential and whatever
    # verifies it, without every test having to construct/override its own
    # `Settings`. Never a real secret -- see
    # docs/architecture/worker-identity-and-security.md. The
    # production-fail-closed-when-missing path
    # (`worker_credentials.resolve_worker_pepper`) is exercised by
    # constructing a bespoke `Settings(app_env=AppEnv.PRODUCTION, ...)`
    # directly in its own test, not through this shared environment.
    "WORKER_CREDENTIAL_PEPPER": "test-only-worker-pepper-not-a-real-secret",
}

for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

from app.main import app  # noqa: E402, I001  (must follow env setup above)


_ANYIO_RUN_SYNC = anyio.to_thread.run_sync


@pytest_asyncio.fixture(autouse=True)
async def _allow_anyio_worker_startup() -> AsyncIterator[None]:
    """Keep AnyIO worker execution off pytest-asyncio's root task.

    With the locked pytest-asyncio/AnyIO versions, direct root-task calls to
    ``anyio.to_thread.run_sync`` can stall before a worker receives its first
    item.  FastAPI's synchronous dependencies and Starlette's ``UploadFile``
    both use that primitive, so the failure prevents existing ASGI tests from
    exercising their assertions.  Production servers already invoke requests
    in child tasks; this test-only shim recreates that scheduling shape.
    """

    async def run_sync_in_child_task(*args: object, **kwargs: object) -> object:
        task = asyncio.create_task(_ANYIO_RUN_SYNC(*args, **kwargs))
        await asyncio.sleep(0.1)
        return await task

    original = anyio.to_thread.run_sync
    anyio.to_thread.run_sync = run_sync_in_child_task  # type: ignore[assignment]
    try:
        yield
    finally:
        anyio.to_thread.run_sync = original


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # `raise_app_exceptions=False` (httpx's default is `True`) makes this
    # client behave like a real HTTP client / real ASGI server (uvicorn):
    # it returns whatever response the app actually sent, even for a
    # request that raised a genuinely unhandled exception. httpx's default
    # instead re-raises that exception to the *caller* of `client.get(...)`
    # -- which is Starlette's own documented behavior for such exceptions
    # (`ServerErrorMiddleware` sends the safe response, then deliberately
    # re-raises purely so a real server or test harness can additionally
    # observe/log it -- see `app/core/errors.py`'s module docstring). With
    # the default, a test exercising that path would see the `await
    # client.get(...)` call itself raise instead of returning a `Response`,
    # which looks like "the safe envelope was never sent" when it actually
    # was -- confirmed by comparing both transport settings against the
    # same route in `tests/unit/test_error_handling.py`.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
