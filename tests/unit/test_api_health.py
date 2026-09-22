"""CORE-HEALTH-001 / CORE-READY-001: /healthz, /readyz, /api/v1/meta/contracts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.contracts import CONTRACT_VERSIONS
from app.dependencies.services import HealthCheckMap, get_health_checks
from app.main import app
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.access_control.models import WorkerCredentialRecord, WorkerCredentialStatus
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("simulated dependency failure")


@pytest.fixture
def fake_ac_repository() -> Iterator[FakeAccessControlRepository]:
    """A real (fake-backed) `AccessControlRepository` so the new worker-
    liveness query in `/readyz` (Gap-Closure WP-6, G17) gets deterministic
    data instead of failing against an unreachable real Postgres. One
    shared instance per test, so a test can seed data before requesting."""
    repository = FakeAccessControlRepository()
    app.dependency_overrides[get_access_control_repository] = lambda: repository
    yield repository
    app.dependency_overrides.pop(get_access_control_repository, None)


@pytest.fixture
def all_healthy(fake_ac_repository: FakeAccessControlRepository) -> Iterator[None]:
    def _override() -> HealthCheckMap:
        return {"postgres": _ok, "neo4j": _ok, "redis": _ok, "minio": _ok}

    app.dependency_overrides[get_health_checks] = _override
    yield
    app.dependency_overrides.pop(get_health_checks, None)


@pytest.fixture
def neo4j_unhealthy(fake_ac_repository: FakeAccessControlRepository) -> Iterator[None]:
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
            "worker_process_liveness": {
                "status": "observed",
                "total_workers": 0,
                "by_liveness": {"active": 0, "stale": 0, "never_seen": 0},
            },
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


async def test_readyz_worker_process_liveness_reflects_real_heartbeat_data(
    client: AsyncClient,
    all_healthy: None,
    fake_ac_repository: FakeAccessControlRepository,
) -> None:
    """Gap-Closure WP-6 (G17): counts are derived from real seeded
    `last_seen_at` values, not hardcoded."""
    now = datetime.now(UTC)

    async def _seed(*, last_seen_at: datetime | None) -> None:
        await fake_ac_repository.create_worker_credential(
            WorkerCredentialRecord(
                worker_id=uuid4(),
                display_name="test-worker",
                status=WorkerCredentialStatus.ACTIVE,
                allowed_processor_names=("fir_report_text_v1",),
                credential_digest="irrelevant-for-this-test",
                created_at=now,
                rotated_at=None,
                revoked_at=None,
                last_seen_at=last_seen_at,
            )
        )

    await _seed(last_seen_at=now)  # active
    await _seed(last_seen_at=now - timedelta(hours=1))  # stale (default threshold is 120s)
    await _seed(last_seen_at=None)  # never_seen

    response = await client.get("/readyz")
    assert response.status_code == 200
    liveness = response.json()["components"]["worker_control_plane"]["worker_process_liveness"]
    assert liveness["status"] == "observed"
    assert liveness["total_workers"] == 3
    assert liveness["by_liveness"] == {"active": 1, "stale": 1, "never_seen": 1}


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


# --- Gap-Closure re-close (G16): GET /metrics -------------------------------


async def test_metrics_returns_prometheus_text_with_no_case_data(
    client: AsyncClient, all_healthy: None
) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE tracex_up gauge" in body
    assert "tracex_up 1" in body
    assert "# TYPE tracex_dependency_up gauge" in body
    assert 'tracex_dependency_up{dependency="postgres"} 1' in body
    # Never a case_id, evidence_id, or any other case-scoped label/value.
    for forbidden in ("case_id", "evidence_id", "password", "secret", "token"):
        assert forbidden not in body


async def test_metrics_reflects_dependency_failures(
    client: AsyncClient, neo4j_unhealthy: None
) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert 'tracex_dependency_up{dependency="neo4j"} 0' in response.text
    assert 'tracex_dependency_up{dependency="postgres"} 1' in response.text


async def test_metrics_worker_counts_reflect_real_seeded_data(
    client: AsyncClient,
    all_healthy: None,
    fake_ac_repository: FakeAccessControlRepository,
) -> None:
    now = datetime.now(UTC)
    await fake_ac_repository.create_worker_credential(
        WorkerCredentialRecord(
            worker_id=uuid4(),
            display_name="active-worker",
            status=WorkerCredentialStatus.ACTIVE,
            allowed_processor_names=("fir_report_text_v1",),
            credential_digest="irrelevant",
            created_at=now,
            rotated_at=None,
            revoked_at=None,
            last_seen_at=now,
        )
    )
    await fake_ac_repository.create_worker_credential(
        WorkerCredentialRecord(
            worker_id=uuid4(),
            display_name="revoked-worker",
            status=WorkerCredentialStatus.REVOKED,
            allowed_processor_names=("fir_report_text_v1",),
            credential_digest="irrelevant-2",
            created_at=now,
            rotated_at=None,
            revoked_at=now,
            last_seen_at=now - timedelta(hours=2),
        )
    )

    response = await client.get("/metrics")
    body = response.text
    assert 'tracex_worker_credentials_total{status="active"} 1' in body
    assert 'tracex_worker_credentials_total{status="revoked"} 1' in body
    assert 'tracex_worker_liveness_total{liveness="active"} 1' in body
    assert 'tracex_worker_liveness_total{liveness="stale"} 1' in body
