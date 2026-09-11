"""`POST /api/v1/internal/worker-jobs/{job_id}/renew` (Phase 2 closeout) --
lease-renewal heartbeat for a worker whose real processing (a long video
analysis, a large graph-projection batch) may outlast the lease window it
was claimed under.

Mirrors `test_worker_input_api.py`'s override/fixture pattern exactly:
fakes for the repository, `require_worker_principal` overridden for the
main-flow tests. Also covers `service.renew_claim` directly (no HTTP layer)
for the same verification-ordering scenarios, complementing the HTTP tests.
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
from app.modules.evidence_lifecycle.errors import InvalidClaimTokenError
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.models import WorkerJobRecord
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.factories import make_evidence_record
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_CLAIM_TOKEN = "the-real-claim-token"  # noqa: S105
_DEFAULT_LEASE = object()

_TEST_WORKER_PRINCIPAL = WorkerPrincipal(
    worker_id=uuid4(),
    display_name="test-worker",
    allowed_processor_names=("media_detection_v1",),
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
    # `renew_claim`/the real route compare against real `datetime.now(UTC)`,
    # not an injectable clock -- mirrors `test_worker_input_api.py`'s
    # identical reasoning.
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
        source_type=SourceType.IMAGE,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:media_detection_v1:1.0.0",
        input_object_uri=evidence.object_uri,
        requested_at=FIXED_TIME,
        status=status,
        queued_at=FIXED_TIME,
        dispatched_at=FIXED_TIME,
        claimed_at=FIXED_TIME if status is not WorkerStatus.QUEUED else None,
        lease_expires_at=lease_expires_at,
        claimed_by="media_detection_v1" if status is not WorkerStatus.QUEUED else None,
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


# --- HTTP layer ---------------------------------------------------------------


async def test_valid_renewal_extends_the_lease(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    original_lease = job.lease_expires_at
    assert original_lease is not None

    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == str(job.job_id)
    new_lease = datetime.fromisoformat(body["lease_expires_at"])
    assert new_lease > original_lease
    assert evidence_repository.jobs[job.job_id].lease_expires_at == new_lease


async def test_renewal_never_leaks_the_claim_token_or_an_object_uri(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 200, response.text
    body_text = response.text
    assert _CLAIM_TOKEN not in body_text
    assert "object_uri" not in body_text
    assert set(response.json().keys()) == {"job_id", "lease_expires_at"}


async def test_renewal_rejects_a_missing_claim_token_header(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    response = await client.post(f"/api/v1/internal/worker-jobs/{job.job_id}/renew")
    assert response.status_code == 401


async def test_renewal_rejects_an_unknown_job(client: AsyncClient) -> None:
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{uuid4()}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_renewal_rejects_a_wrong_claim_token(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository)
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": "wrong-token"},
    )
    assert response.status_code == 401


async def test_renewal_rejects_a_different_workers_valid_claim_token(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository, claimed_by_worker_id=uuid4())
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_renewal_rejects_a_not_currently_running_job(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(evidence_repository, status=WorkerStatus.SUCCEEDED)
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_renewal_rejects_an_already_expired_lease(
    client: AsyncClient, evidence_repository: FakeEvidenceLifecycleRepository
) -> None:
    job = _seed_claimed_job(
        evidence_repository, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


# --- service layer (verification ordering, no HTTP) ---------------------------


def _service(repository: FakeEvidenceLifecycleRepository) -> EvidenceLifecycleService:
    return EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
    )


def _context() -> UploadContext:
    return UploadContext(now=datetime.now(UTC), request_id="req-test")


async def test_service_renew_claim_extends_the_lease() -> None:
    repository = FakeEvidenceLifecycleRepository()
    job = _seed_claimed_job(repository)
    service = _service(repository)

    new_lease = await service.renew_claim(
        job_id=job.job_id,
        claim_token=_CLAIM_TOKEN,
        context=_context(),
        worker_id=_TEST_WORKER_PRINCIPAL.worker_id,
    )

    assert new_lease > job.lease_expires_at  # type: ignore[operator]


async def test_service_renew_claim_rejects_worker_identity_mismatch() -> None:
    repository = FakeEvidenceLifecycleRepository()
    job = _seed_claimed_job(repository)
    service = _service(repository)

    with pytest.raises(InvalidClaimTokenError):
        await service.renew_claim(
            job_id=job.job_id, claim_token=_CLAIM_TOKEN, context=_context(), worker_id=uuid4()
        )


async def test_service_renew_claim_rejects_a_queued_never_claimed_job() -> None:
    """A job with a genuinely `None` `claim_token_hash` (never claimed at all) --
    distinct from `_seed_claimed_job`, whose helper always synthesizes a real
    hash even when asked for `None`, since every *other* scenario in this file
    needs a claimed job."""
    repository = FakeEvidenceLifecycleRepository()
    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(case_id=case_id, evidence_id=evidence_id)
    repository.evidence[evidence.evidence_id] = evidence
    job = WorkerJobRecord(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=SourceType.IMAGE,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:media_detection_v1:1.0.0",
        input_object_uri=evidence.object_uri,
        requested_at=FIXED_TIME,
        status=WorkerStatus.QUEUED,
        queued_at=FIXED_TIME,
        dispatched_at=FIXED_TIME,
        claimed_at=None,
        lease_expires_at=None,
        claimed_by=None,
        claimed_by_worker_id=None,
        claim_token_hash=None,
        last_error_code=None,
        last_error_message=None,
        last_error_retryable=None,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )
    repository.jobs[job.job_id] = job
    service = _service(repository)

    with pytest.raises(InvalidClaimTokenError):
        await service.renew_claim(
            job_id=job.job_id,
            claim_token=_CLAIM_TOKEN,
            context=_context(),
            worker_id=_TEST_WORKER_PRINCIPAL.worker_id,
        )
