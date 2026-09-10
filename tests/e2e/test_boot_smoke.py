"""End-to-end smoke test: the app boots and its three Phase 1 endpoints respond.

Phase 1 has no frontend and no authenticated user flow, so this is the
smallest meaningful "e2e" in this repo: a real ASGI app instance, hit over
real HTTP semantics via httpx, exercising healthz -> readyz -> contracts in
sequence. See scenario 17 (FastAPI startup smoke test) in docs/qa/test-matrix.md.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.dependencies.services import HealthCheckMap, get_health_checks
from app.main import app


@pytest.fixture
def all_dependencies_healthy() -> Iterator[None]:
    async def _ok() -> None:
        return None

    def _override() -> HealthCheckMap:
        return {"postgres": _ok, "neo4j": _ok, "redis": _ok, "minio": _ok}

    app.dependency_overrides[get_health_checks] = _override
    yield
    app.dependency_overrides.pop(get_health_checks, None)


async def test_app_boots_and_serves_health_readiness_and_contracts(
    client: AsyncClient, all_dependencies_healthy: None
) -> None:
    healthz = await client.get("/healthz")
    assert healthz.status_code == 200

    readyz = await client.get("/readyz")
    assert readyz.status_code == 200

    contracts = await client.get("/api/v1/meta/contracts")
    assert contracts.status_code == 200
    assert contracts.json()["evidence_record"] == "EvidenceRecordV1"
