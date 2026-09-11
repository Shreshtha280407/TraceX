"""HTTP-layer tests: `/api/v1/internal/worker-jobs/*` wiring, auth, safe responses.

Mirrors `test_evidence_api.py`'s override pattern. `require_worker_principal`
is overridden directly for the main flow tests (its own docstring names
this as the intended test pattern); the fail-closed-when-unconfigured and
wrong-secret paths are exercised through the *real* dependency instead, to
prove the boundary itself works, not just that tests can bypass it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerStatus
from app.main import app
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.evidence_lifecycle.dependencies import (
    WorkerPrincipal,
    get_evidence_lifecycle_repository,
    get_job_producer,
    get_object_storage,
    require_worker_principal,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.models import WorkerJobRecord
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import make_observation, make_worker_result

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def evidence_repository() -> FakeEvidenceLifecycleRepository:
    return FakeEvidenceLifecycleRepository()


@pytest.fixture
def job_producer() -> FakeJobProducer:
    return FakeJobProducer()


@pytest.fixture
def _override_worker_dependencies(
    evidence_repository: FakeEvidenceLifecycleRepository, job_producer: FakeJobProducer
) -> Iterator[None]:
    app.dependency_overrides[get_evidence_lifecycle_repository] = lambda: evidence_repository
    app.dependency_overrides[get_object_storage] = lambda: FakeObjectStorage()
    app.dependency_overrides[get_job_producer] = lambda: job_producer
    app.dependency_overrides[require_worker_principal] = lambda: WorkerPrincipal()
    app.dependency_overrides[get_access_control_repository] = FakeAccessControlRepository
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_worker_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _seed_queued_job(
    repository: FakeEvidenceLifecycleRepository,
    *,
    processor_name: str = "cdr_generic_v1",
    processor_version: str = "1.0.0",
) -> WorkerJobRecord:
    case_id = uuid4()
    evidence_id = uuid4()
    job = WorkerJobRecord(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=SourceType.CDR,
        processor_name=processor_name,
        processor_version=processor_version,
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:{processor_name}:{processor_version}",
        input_object_uri=f"cases/{case_id}/evidence/{evidence_id}/original",
        requested_at=FIXED_TIME,
        status=WorkerStatus.QUEUED,
        queued_at=FIXED_TIME,
        dispatched_at=None,
        claimed_at=None,
        lease_expires_at=None,
        claimed_by=None,
        claim_token_hash=None,
        last_error_code=None,
        last_error_message=None,
        last_error_retryable=None,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )
    repository.jobs[job.job_id] = job
    return job


# --- Claim + submit full round trip ------------------------------------------


async def test_claim_and_submit_full_round_trip(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_queued_job(evidence_repository)

    claim = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        json={"processor_name": job.processor_name, "processor_version": job.processor_version},
    )
    assert claim.status_code == 200, claim.text
    body = claim.json()
    assert body["job"]["job_id"] == str(job.job_id)
    assert body["job"]["input_object_uri"] == job.input_object_uri
    claim_token = body["claim_token"]
    assert claim_token

    observation = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        observations=[observation],
        error=None,
    )
    submit = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/result",
        headers={"X-Claim-Token": claim_token, "Content-Type": "application/json"},
        content=result.model_dump_json(),
    )
    assert submit.status_code == 200, submit.text
    ack = submit.json()
    assert ack["status"] == "succeeded"
    assert ack["observation_count"] == 1
    assert ack["observation_ids"] == [str(observation.observation_id)]


async def test_claim_returns_no_work_when_nothing_eligible(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 200
    assert response.json() == {"job": None, "claim_token": None, "lease_expires_at": None}


async def test_submit_with_wrong_claim_token_is_401(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_queued_job(evidence_repository)
    await client.post(
        "/api/v1/internal/worker-jobs/claim",
        json={"processor_name": job.processor_name, "processor_version": job.processor_version},
    )
    result = make_worker_result(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id, error=None
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/result",
        headers={"X-Claim-Token": "definitely-wrong", "Content-Type": "application/json"},
        content=result.model_dump_json(),
    )
    assert response.status_code == 401


async def test_submit_without_claim_token_header_is_401(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_queued_job(evidence_repository)
    result = make_worker_result(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id, error=None
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/result",
        content=result.model_dump_json(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


async def test_claim_response_never_exposes_internal_details(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    _seed_queued_job(evidence_repository)
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
    )
    text = response.text.lower()
    assert "minio" not in text
    assert "secret" not in text
    assert "password" not in text


# --- Fail-closed worker-principal boundary (real dependency, not overridden) --


async def test_worker_endpoints_fail_closed_when_unconfigured() -> None:
    """No override for `require_worker_principal`: exercises the real dependency.

    `tests/conftest.py` sets no `WORKER_SHARED_SECRET`, so `Settings()`
    resolves it to `None` -- the documented fail-closed default.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/internal/worker-jobs/claim",
            json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
        )
    assert response.status_code == 503
