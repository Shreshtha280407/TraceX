"""CORE-HEALTH-001 / CORE-READY-001: /healthz, /readyz, /api/v1/meta/contracts."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.contracts import CONTRACT_VERSIONS
from app.dependencies.services import HealthCheckMap, get_health_checks
from app.main import app


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("simulated dependency failure")


@pytest.fixture
def all_healthy() -> Iterator[None]:
    def _override() -> HealthCheckMap:
        return {"postgres": _ok, "neo4j": _ok, "redis": _ok, "minio": _ok}

    app.dependency_overrides[get_health_checks] = _override
    yield
    app.dependency_overrides.pop(get_health_checks, None)


@pytest.fixture
def neo4j_unhealthy() -> Iterator[None]:
    def _override() -> HealthCheckMap:
        return {"postgres": _ok, "neo4j": _fail, "redis": _ok, "minio": _ok}

    app.dependency_overrides[get_health_checks] = _override
    yield
    app.dependency_overrides.pop(get_health_checks, None)


async def test_healthz_returns_200(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "service": "tracex-api", "version": "0.1.0"}


async def test_readyz_returns_200_when_all_dependencies_healthy(
    client: AsyncClient, all_healthy: None
) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"] == {
        "postgres": "ok",
        "neo4j": "ok",
        "redis": "ok",
        "minio": "ok",
    }
    assert body["components"] == {
        "worker_control_plane": {
            "status": "ok",
            "dependencies": ["postgres", "redis", "minio"],
            "worker_process_liveness": "not_observed",
        },
        "graph_projection": {
            "status": "ok",
            "dependencies": ["postgres", "neo4j"],
        },
        "correlation_outbox": {
            "status": "ok",
            "dependencies": ["postgres", "neo4j"],
        },
    }


async def test_readyz_returns_503_with_safe_body_when_dependency_fails(
    client: AsyncClient, neo4j_unhealthy: None
) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()

    assert body["error"]["code"] == "dependency_unavailable"
    assert body["dependencies"]["neo4j"] == "unavailable"
    assert body["dependencies"]["postgres"] == "ok"
    assert body["components"]["worker_control_plane"]["status"] == "ok"
    assert body["components"]["graph_projection"]["status"] == "unavailable"
    assert body["components"]["correlation_outbox"]["status"] == "unavailable"

    # Never leak connection strings, credentials, or exception internals.
    raw = response.text
    assert "test-only" not in raw
    assert "postgresql" not in raw
    assert "ConnectionError" not in raw
    assert "Traceback" not in raw


async def test_contracts_endpoint_reports_exact_supported_versions(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/meta/contracts")
    assert response.status_code == 200
    assert response.json() == CONTRACT_VERSIONS
    assert response.json() == {
        "evidence_record": "EvidenceRecordV1",
        "observation": "ObservationV1",
        "entity": "EntityV1",
        "event": "EventV1",
        "worker_job": "WorkerJobV1",
        "worker_result": "WorkerResultV1",
        "observation_batch_submission": "ObservationBatchSubmissionV1",
        "observation_batch_receipt": "ObservationBatchReceiptV1",
        "transformation_provenance": "TransformationProvenanceV1",
    }
