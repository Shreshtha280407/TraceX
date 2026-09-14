"""Focused unit coverage for Phase 4 LAN worker transport controls."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings
from app.modules.evidence_lifecycle.worker_security import WorkerControlPlaneGuardMiddleware


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"ok": True})(scope, receive, send)


def _settings(**overrides: object) -> Settings:
    return Settings(
        postgres_dsn="postgresql+asyncpg://user:password@localhost:5432/db",
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="password",
        redis_url="redis://localhost:6379/0",
        minio_endpoint="localhost:9000",
        minio_access_key="access",
        minio_secret_key="secret",
        auth_jwt_secret="x" * 32,
        **overrides,
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app: ASGIApp = WorkerControlPlaneGuardMiddleware(
        _ok_app,
        settings=_settings(worker_secure_transport_required=True),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://worker.test"
    ) as value:
        yield value


async def test_secure_mode_rejects_plain_http_worker_control_request(client: AsyncClient) -> None:
    response = await client.post("/api/v1/internal/worker-jobs/claim")
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "secure worker transport required"


async def test_secure_mode_accepts_forwarded_https_only_from_trusted_proxy() -> None:
    app: ASGIApp = WorkerControlPlaneGuardMiddleware(
        _ok_app,
        settings=_settings(
            worker_secure_transport_required=True,
            worker_trusted_proxy_ips="127.0.0.1",
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://x"
    ) as c:
        response = await c.post(
            "/api/v1/internal/worker-jobs/claim", headers={"X-Forwarded-Proto": "https"}
        )
    assert response.status_code == 200


async def test_declared_oversized_control_payload_is_rejected_before_application() -> None:
    app: ASGIApp = WorkerControlPlaneGuardMiddleware(
        _ok_app,
        settings=_settings(worker_internal_request_max_bytes=1024),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        response = await c.post(
            "/api/v1/internal/worker-jobs/claim",
            content=b"x" * 1025,
            headers={"Content-Length": "1025"},
        )
    assert response.status_code == 413
    assert "payload too large" in response.text


def test_heartbeat_threshold_cannot_exceed_lease() -> None:
    with pytest.raises(ValueError, match="worker_heartbeat_stale_seconds"):
        _settings(worker_lease_seconds=60, worker_heartbeat_stale_seconds=61)
