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

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerStatus
from app.core.config import get_settings
from app.main import app
from app.modules.access_control.dependencies import get_access_control_repository
from app.modules.evidence_lifecycle.dependencies import (
    WorkerPrincipal,
    get_evidence_lifecycle_repository,
    get_evidence_lifecycle_service,
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
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def _override_worker_dependencies(
    evidence_repository: FakeEvidenceLifecycleRepository,
    ac_repository: FakeAccessControlRepository,
) -> Iterator[None]:
    # FastAPI otherwise executes synchronous test overrides in AnyIO's
    # worker-thread bridge.  That bridge is the source of the historic
    # stalled route test under pytest-asyncio; async overrides keep the
    # entirely in-memory HTTP test on its owning event loop.
    async def fake_repository() -> FakeEvidenceLifecycleRepository:
        return evidence_repository

    async def fake_service() -> EvidenceLifecycleService:
        return _service(evidence_repository)

    async def fake_storage() -> FakeObjectStorage:
        return FakeObjectStorage()

    async def fake_job_producer() -> FakeJobProducer:
        return FakeJobProducer()

    async def fake_principal() -> WorkerPrincipal:
        return _TEST_WORKER_PRINCIPAL

    async def fake_access_repository() -> FakeAccessControlRepository:
        return ac_repository

    async def test_settings():
        return get_settings()

    app.dependency_overrides[get_evidence_lifecycle_repository] = fake_repository
    # Override the composed service directly.  Resolving its nested production
    # dependency graph in this HTTP unit test can retain the production
    # async-engine dependency while an ASGI request is in flight; the route
    # is meant to exercise the HTTP/auth boundary against the in-memory
    # lifecycle implementation, not the external infrastructure wiring.
    app.dependency_overrides[get_evidence_lifecycle_service] = fake_service
    app.dependency_overrides[get_object_storage] = fake_storage
    app.dependency_overrides[get_job_producer] = fake_job_producer
    app.dependency_overrides[require_worker_principal] = fake_principal
    app.dependency_overrides[get_access_control_repository] = fake_access_repository
    app.dependency_overrides[get_settings] = test_settings
    yield
    app.dependency_overrides.clear()


async def _post_worker_route(url: str, **kwargs: Any) -> Any:
    """Issue one real ASGI request without a pytest-managed async client.

    The prior ``client`` fixture stalled the installed pytest-asyncio runner
    before the test coroutine began.  Opening the connectionless ASGI
    transport inside the coroutine keeps the test a true HTTP-route test.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as value:
        request = asyncio.create_task(value.post(url, **kwargs))
        # Let the nested ASGI task establish its AnyIO worker-thread bridge
        # before the pytest-asyncio root task awaits its response.
        await asyncio.sleep(0)
        return await request


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
        max_attempts=5,
        idempotency_key=f"{case_id}:{evidence_id}:media_detection_v1:1.0.0",
        input_object_uri=evidence.object_uri,
        requested_at=FIXED_TIME,
        status=status,
        queued_at=FIXED_TIME,
        dispatched_at=FIXED_TIME,
        # Realistic relative to `datetime.now(UTC)`, not `FIXED_TIME` --
        # `renew_claim`'s new absolute-lease-lifetime cap is computed from
        # `claimed_at`, and `renew_claim` itself compares against real
        # current time (see this function's own comment above), so a
        # `claimed_at` fixed in the past would make every renewal look like
        # it's already past its ceiling, regardless of `lease_expires_at`.
        claimed_at=datetime.now(UTC) if status is not WorkerStatus.QUEUED else None,
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
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository)
    original_lease = job.lease_expires_at
    assert original_lease is not None

    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == str(job.job_id)
    new_lease = datetime.fromisoformat(body["lease_expires_at"])
    assert new_lease > original_lease
    assert evidence_repository.jobs[job.job_id].lease_expires_at == new_lease


async def test_valid_renewal_records_a_worker_job_lease_renewed_audit_event(
    evidence_repository: FakeEvidenceLifecycleRepository,
    ac_repository: FakeAccessControlRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository)

    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 200, response.text

    events = [e for e in ac_repository.audit_events if e.event_type == "worker_job_lease_renewed"]
    assert len(events) == 1
    assert events[0].metadata_safe_json["job_id"] == str(job.job_id)
    assert events[0].metadata_safe_json["worker_id"] == str(_TEST_WORKER_PRINCIPAL.worker_id)
    assert events[0].case_id_nullable == job.case_id
    assert _CLAIM_TOKEN not in str(events[0].metadata_safe_json)


async def test_renewal_never_leaks_the_claim_token_or_an_object_uri(
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository)
    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 200, response.text
    body_text = response.text
    assert _CLAIM_TOKEN not in body_text
    assert "object_uri" not in body_text
    assert set(response.json().keys()) == {"job_id", "lease_expires_at"}


async def test_renewal_rejects_a_missing_claim_token_header(
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository)
    response = await _post_worker_route(f"/api/v1/internal/worker-jobs/{job.job_id}/renew")
    assert response.status_code == 401


async def test_renewal_rejects_an_unknown_job(_override_worker_dependencies: None) -> None:
    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{uuid4()}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_renewal_rejects_a_wrong_claim_token(
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository)
    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": "wrong-token"},
    )
    assert response.status_code == 401


async def test_renewal_rejects_a_different_workers_valid_claim_token(
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository, claimed_by_worker_id=uuid4())
    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_renewal_rejects_a_not_currently_running_job(
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(evidence_repository, status=WorkerStatus.SUCCEEDED)
    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_renewal_rejects_an_already_expired_lease(
    evidence_repository: FakeEvidenceLifecycleRepository,
    _override_worker_dependencies: None,
) -> None:
    job = _seed_claimed_job(
        evidence_repository, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    response = await _post_worker_route(
        f"/api/v1/internal/worker-jobs/{job.job_id}/renew",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


# --- service layer (verification ordering, no HTTP) ---------------------------


def _service(
    repository: FakeEvidenceLifecycleRepository, *, worker_lease_max_seconds: int = 3600
) -> EvidenceLifecycleService:
    return EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_max_seconds=worker_lease_max_seconds,
    )


def _context() -> UploadContext:
    return UploadContext(now=datetime.now(UTC), request_id="req-test")


async def test_service_renew_claim_extends_the_lease() -> None:
    repository = FakeEvidenceLifecycleRepository()
    job = _seed_claimed_job(repository)
    service = _service(repository)

    renewal = await service.renew_claim(
        job_id=job.job_id,
        claim_token=_CLAIM_TOKEN,
        context=_context(),
        worker_id=_TEST_WORKER_PRINCIPAL.worker_id,
    )

    assert renewal.lease_expires_at > job.lease_expires_at  # type: ignore[operator]
    assert renewal.case_id == job.case_id
    assert renewal.evidence_id == job.evidence_id
    assert renewal.attempt == job.attempt


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
        max_attempts=5,
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


# --- Absolute maximum lease lifetime (Phase 3, Aditya) -----------------------


async def test_renewal_is_capped_at_the_absolute_maximum_lease_lifetime() -> None:
    """A renewal request for more time than the ceiling allows is granted only up to
    the ceiling -- `claimed_at + worker_lease_max_seconds` -- never the full requested
    extension."""
    repository = FakeEvidenceLifecycleRepository()
    now = datetime.now(UTC)
    job = _seed_claimed_job(
        repository,
        lease_expires_at=now + timedelta(seconds=30),
    )
    # This job's `claimed_at` was set to (approximately) `now` by
    # `_seed_claimed_job`. A tiny ceiling (100s) makes the cap bite well
    # before the requested 5-minute renewal would otherwise land.
    service = _service(repository, worker_lease_max_seconds=100)

    renewal = await service.renew_claim(
        job_id=job.job_id,
        claim_token=_CLAIM_TOKEN,
        context=UploadContext(now=now, request_id="req-test"),
        worker_id=_TEST_WORKER_PRINCIPAL.worker_id,
    )

    assert job.claimed_at is not None
    ceiling = job.claimed_at + timedelta(seconds=100)
    assert renewal.lease_expires_at == ceiling
    # The default per-call lease extension (5 minutes, `_seed_claimed_job`'s
    # own default doesn't apply to `renew_claim`'s own `worker_lease_seconds`
    # default of 300s) would have landed well past the ceiling had it not
    # been capped.
    assert renewal.lease_expires_at < now + timedelta(minutes=5)


async def test_renewal_eventually_stops_extending_once_the_ceiling_is_reached() -> None:
    """Repeated renewals never push the lease past the absolute ceiling -- once real
    time passes it, the (uncapped-further) lease naturally falls behind `now` and
    renewal starts being rejected exactly as if the worker had stopped heartbeating."""
    repository = FakeEvidenceLifecycleRepository()
    now = datetime.now(UTC)
    job = _seed_claimed_job(repository, lease_expires_at=now + timedelta(seconds=30))
    service = _service(repository, worker_lease_max_seconds=60)

    first = await service.renew_claim(
        job_id=job.job_id,
        claim_token=_CLAIM_TOKEN,
        context=UploadContext(now=now, request_id="req-test"),
        worker_id=_TEST_WORKER_PRINCIPAL.worker_id,
    )
    assert job.claimed_at is not None
    assert first.lease_expires_at == job.claimed_at + timedelta(seconds=60)

    # Time passes the absolute ceiling entirely -- the lease (still capped at
    # the ceiling) is now in the past relative to `now`, so renewal is
    # rejected exactly like any other expired lease, never silently revived.
    past_ceiling = job.claimed_at + timedelta(seconds=61)
    with pytest.raises(InvalidClaimTokenError) as excinfo:
        await service.renew_claim(
            job_id=job.job_id,
            claim_token=_CLAIM_TOKEN,
            context=UploadContext(now=past_ceiling, request_id="req-test"),
            worker_id=_TEST_WORKER_PRINCIPAL.worker_id,
        )
    assert excinfo.value.reason == "lease_expired"
