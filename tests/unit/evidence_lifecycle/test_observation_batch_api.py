"""`POST /api/v1/internal/worker-jobs/{job_id}/observations` (Phase 3, Nipun) --
HTTP-layer wiring, auth, scope, and safe-response tests for the batch-submission
route. Mirrors `test_worker_lease_renewal.py`'s override/fixture pattern exactly.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

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
from tests.fixtures.evidence_lifecycle.factories import make_evidence_record
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import make_batch_progress, make_observation_batch_submission

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_CLAIM_TOKEN = "the-real-claim-token"  # noqa: S105
_DEFAULT_LEASE = object()

_TEST_WORKER_PRINCIPAL = WorkerPrincipal(
    worker_id=uuid4(), display_name="test-worker", allowed_processor_names=("cdr_generic_v1",)
)


@pytest.fixture
def evidence_repository() -> FakeEvidenceLifecycleRepository:
    return FakeEvidenceLifecycleRepository()


@pytest.fixture
def _override_worker_dependencies(
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> Iterator[None]:
    app.dependency_overrides[get_evidence_lifecycle_repository] = lambda: evidence_repository
    app.dependency_overrides[get_object_storage] = FakeObjectStorage
    app.dependency_overrides[get_job_producer] = FakeJobProducer
    app.dependency_overrides[require_worker_principal] = lambda: _TEST_WORKER_PRINCIPAL
    app.dependency_overrides[get_access_control_repository] = FakeAccessControlRepository
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_worker_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _seed_claimed_job(
    repository: FakeEvidenceLifecycleRepository,
    *,
    status: WorkerStatus = WorkerStatus.RUNNING,
    lease_expires_at: datetime | None | object = _DEFAULT_LEASE,
    claim_token_hash: str | None = None,
    claimed_by_worker_id: UUID | None = _TEST_WORKER_PRINCIPAL.worker_id,
) -> WorkerJobRecord:
    if lease_expires_at is _DEFAULT_LEASE:
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    repository.evidence[evidence.evidence_id] = evidence

    if claim_token_hash is None:
        claim_token_hash = hashlib.sha256(_CLAIM_TOKEN.encode("utf-8")).hexdigest()

    job = WorkerJobRecord(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=SourceType.CDR,
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:cdr_generic_v1:1.0.0",
        input_object_uri=evidence.object_uri,
        requested_at=FIXED_TIME,
        status=status,
        queued_at=FIXED_TIME,
        dispatched_at=FIXED_TIME,
        claimed_at=FIXED_TIME if status is not WorkerStatus.QUEUED else None,
        lease_expires_at=lease_expires_at,
        claimed_by="cdr_generic_v1" if status is not WorkerStatus.QUEUED else None,
        claimed_by_worker_id=claimed_by_worker_id,
        claim_token_hash=claim_token_hash,
        last_error_code=None,
        last_error_message=None,
        last_error_retryable=None,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )
    repository.jobs[job.job_id] = job
    return job


# --- Scenario 18: a valid claimed worker can submit its own job batch -------


async def test_valid_claimed_worker_can_submit_its_own_job_batch(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )

    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == str(job.job_id)
    assert body["batch_id"] == submission.batch_id
    assert body["status"] == "accepted"
    assert body["accepted_observation_count"] == 1
    assert body["progress"]["stage"] == submission.progress.stage
    assert len(evidence_repository.observations) == 1


# --- Scenario 19: wrong worker / wrong token / expired lease / wrong job ----


async def test_submit_rejects_a_missing_claim_token_header(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        content=submission.model_dump_json(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert evidence_repository.observations == {}


async def test_submit_rejects_a_wrong_claim_token(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": "definitely-wrong", "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 401
    assert evidence_repository.observations == {}


async def test_submit_rejects_a_different_workers_valid_claim_token(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository, claimed_by_worker_id=uuid4())
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 401
    assert evidence_repository.observations == {}


async def test_submit_rejects_an_already_expired_lease_for_a_new_batch(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(
        evidence_repository, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 422  # a genuinely new batch needs a currently-valid lease
    assert evidence_repository.observations == {}


async def test_submit_rejects_an_unknown_job(client: AsyncClient) -> None:
    submission = make_observation_batch_submission()
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{uuid4()}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 401


# --- Scenario 20: path/body job_id mismatch is rejected ---------------------


async def test_submit_rejects_path_body_job_id_mismatch(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=uuid4(),  # deliberately does not match the path job_id
        case_id=job.case_id,
        evidence_id=job.evidence_id,
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 422
    assert evidence_repository.observations == {}


# --- Scenario 21: cross-case/cross-evidence submission is rejected ---------


async def test_submit_rejects_cross_case_submission_with_no_persistence(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=uuid4(),  # not this job's real case
        evidence_id=job.evidence_id,
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 422
    assert evidence_repository.observations == {}
    assert evidence_repository.observation_batches == {}


async def test_submit_rejects_cross_evidence_submission_with_no_persistence(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=uuid4(),  # not this job's real evidence
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 422
    assert evidence_repository.observations == {}
    assert evidence_repository.observation_batches == {}


# --- Scenario 22: error bodies never leak secrets/object URIs/stack traces --


async def test_unauthorized_response_never_leaks_unsafe_details(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": "wrong", "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )
    assert response.status_code == 401
    text = response.text.lower()
    for forbidden in ("minio", "secret", "password", "select ", "traceback", "object_uri"):
        assert forbidden not in text
    assert _CLAIM_TOKEN not in response.text


async def test_conflict_response_never_leaks_unsafe_details(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=submission.model_dump_json(),
    )

    different_payload = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id=submission.batch_id,
        idempotency_key=submission.idempotency_key,
        progress=make_batch_progress(batch_sequence=0, units_completed=9),
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/observations",
        headers={"X-Claim-Token": _CLAIM_TOKEN, "Content-Type": "application/json"},
        content=different_payload.model_dump_json(),
    )
    assert response.status_code == 409
    text = response.text.lower()
    for forbidden in ("minio", "secret", "password", "select ", "traceback", "object_uri"):
        assert forbidden not in text
