"""HTTP-layer tests: `GET /api/v1/internal/worker-jobs/{job_id}/input` (Phase 2.2).

Mirrors `test_worker_internal_api.py`'s override pattern exactly: fakes for
repository/storage, `require_worker_principal` overridden for the main flow
tests, the real dependency exercised separately for the fail-closed paths.
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
from app.modules.evidence_lifecycle.storage import FakeObjectStorage, ObjectStream
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.factories import make_evidence_record
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_INPUT_BYTES = b"FIR No. 1/2026 filed at Test Police Station"
_CLAIM_TOKEN = "the-real-claim-token"  # noqa: S105
_DEFAULT_LEASE = object()  # sentinel: "use a real, not-yet-expired lease" (see _seed_claimed_job)


@pytest.fixture
def evidence_repository() -> FakeEvidenceLifecycleRepository:
    return FakeEvidenceLifecycleRepository()


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


_TEST_WORKER_PRINCIPAL = WorkerPrincipal(
    worker_id=uuid4(),
    display_name="test-worker",
    allowed_processor_names=("fir_report_text_v1",),
)


@pytest.fixture
def _override_worker_dependencies(
    evidence_repository: FakeEvidenceLifecycleRepository, storage: FakeObjectStorage
) -> Iterator[None]:
    app.dependency_overrides[get_evidence_lifecycle_repository] = lambda: evidence_repository
    app.dependency_overrides[get_object_storage] = lambda: storage
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
    storage: FakeObjectStorage,
    *,
    status: WorkerStatus = WorkerStatus.RUNNING,
    lease_expires_at: datetime | None | object = _DEFAULT_LEASE,
    claim_token_hash: str | None = None,
    claimed_by_worker_id: UUID | None = _TEST_WORKER_PRINCIPAL.worker_id,
    data: bytes = _INPUT_BYTES,
) -> WorkerJobRecord:
    # The route always compares against the *real* current time
    # (`datetime.now(UTC)`, not an injectable clock -- see
    # `internal_api.py::get_worker_job_input`), so the default lease here
    # must be relative to real "now" too, not the fixed synthetic
    # `FIXED_TIME` every other timestamp in this fixture uses.
    if lease_expires_at is _DEFAULT_LEASE:
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    case_id, evidence_id = uuid4(), uuid4()
    evidence = make_evidence_record(
        case_id=case_id,
        evidence_id=evidence_id,
        content_type="text/plain",
        original_filename="fir_report.txt",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    repository.evidence[evidence.evidence_id] = evidence
    storage.objects[evidence.object_uri] = data

    if claim_token_hash is None:
        claim_token_hash = hashlib.sha256(_CLAIM_TOKEN.encode("utf-8")).hexdigest()

    job = WorkerJobRecord(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=SourceType.DOCUMENT,
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:fir_report_text_v1:1.0.0",
        input_object_uri=evidence.object_uri,
        requested_at=FIXED_TIME,
        status=status,
        queued_at=FIXED_TIME,
        dispatched_at=FIXED_TIME,
        claimed_at=FIXED_TIME if status is not WorkerStatus.QUEUED else None,
        lease_expires_at=lease_expires_at,
        claimed_by="fir_report_text_v1" if status is not WorkerStatus.QUEUED else None,
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


# --- 1: valid claim token, running job, unexpired lease -----------------------


async def test_valid_claim_receives_exact_stored_bytes(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)

    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )

    assert response.status_code == 200, response.text
    assert response.content == _INPUT_BYTES
    # Starlette's `StreamingResponse` appends `; charset=utf-8` to a `text/*`
    # media type automatically -- not something this route controls.
    assert response.headers["content-type"].startswith("text/plain")


# --- 2: a multi-chunk source stream is correctly relayed end to end ---------
#
# Whether the client's transport happens to preserve individual chunk
# boundaries is httpx/ASGI-transport buffering, not something this route
# controls -- the reliable, non-flaky proof that the API itself never
# fully buffers the object before streaming lives at the storage layer:
# `tests/unit/evidence_lifecycle/test_object_storage.py::
# test_stream_response_pulls_chunks_lazily_and_closes_the_response`.


async def test_multi_chunk_source_stream_is_relayed_correctly(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    chunks = [b"a" * 1024, b"b" * 1024, b"c" * 1024]
    pulled: list[bytes] = []

    async def _multi_chunk() -> AsyncIterator[bytes]:
        for chunk in chunks:
            pulled.append(chunk)
            yield chunk

    storage.open_stream = lambda object_key: _fake_open_stream(  # type: ignore[method-assign]
        object_key, _multi_chunk(), sum(len(c) for c in chunks)
    )

    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )

    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert response.headers["content-length"] == str(sum(len(c) for c in chunks))
    assert pulled == chunks  # the route consumed the injected stream, not a substitute


async def _fake_open_stream(
    object_key: str,
    chunks: AsyncIterator[bytes],
    content_length: int,  # noqa: ARG001
) -> ObjectStream:
    return ObjectStream(content_length=content_length, chunks=chunks)


# --- 3/4: worker authentication --------------------------------------------


async def test_missing_worker_authentication_rejected_safely() -> None:
    """No `require_worker_principal` override: exercises the real fail-closed dependency.

    A pepper is configured in the shared test environment (see
    `tests/conftest.py`), so per-worker authentication is genuinely active
    -- a missing `Authorization` header is a `401`.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/api/v1/internal/worker-jobs/{uuid4()}/input",
            headers={"X-Claim-Token": "irrelevant"},
        )
    assert response.status_code == 401


# --- 5: missing / incorrect / malformed claim tokens ------------------------


async def test_missing_claim_token_header_rejected(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    response = await client.get(f"/api/v1/internal/worker-jobs/{job.job_id}/input")
    assert response.status_code == 401


async def test_wrong_claim_token_rejected(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": "definitely-wrong"},
    )
    assert response.status_code == 401


async def test_malformed_oversized_claim_token_rejected(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": "x" * 500},
    )
    assert response.status_code == 401


# --- 6: a claim token cannot access another job's evidence -------------------


async def test_claim_token_cannot_access_a_different_job(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    _seed_claimed_job(evidence_repository, storage)  # job A, owns _CLAIM_TOKEN
    job_b = _seed_claimed_job(
        evidence_repository, storage, claim_token_hash="a" * 64, data=b"job b bytes"
    )

    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job_b.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


# --- 7: expired-lease, queued, and terminal jobs cannot retrieve input -------


async def test_expired_lease_job_cannot_retrieve_input(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(
        evidence_repository, storage, lease_expires_at=FIXED_TIME - timedelta(minutes=1)
    )
    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_queued_never_claimed_job_cannot_retrieve_input(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(
        evidence_repository,
        storage,
        status=WorkerStatus.QUEUED,
        lease_expires_at=None,
        claim_token_hash=None,
    )
    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


async def test_terminal_job_cannot_retrieve_input(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage, status=WorkerStatus.SUCCEEDED)
    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 401


# --- 8: missing MinIO object -> safe error, no storage details leaked -------


async def test_missing_stored_object_returns_safe_error(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    del storage.objects[job.input_object_uri]  # evidence row exists; backing object doesn't

    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )

    assert response.status_code == 500
    text = response.text.lower()
    assert "minio" not in text
    assert job.input_object_uri.lower() not in text
    assert "simulated missing object" not in text  # the raw exception message itself


# --- 9: response headers carry only safe metadata ---------------------------


async def test_response_headers_never_leak_storage_details(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    response = await client.get(
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"X-Claim-Token": _CLAIM_TOKEN},
    )
    assert response.status_code == 200
    header_text = " ".join(f"{k}:{v}" for k, v in response.headers.items()).lower()
    assert job.input_object_uri.lower() not in header_text
    assert "minio" not in header_text
    assert "presigned" not in header_text
    assert _CLAIM_TOKEN.lower() not in header_text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-tracex-evidence-id"] == str(job.evidence_id)
    assert len(response.headers["x-tracex-evidence-sha256"]) == 64
    assert response.headers["x-tracex-source-type"] == "document"


# --- 10: no secret/claim-token leakage in logs -------------------------------


async def test_no_secret_or_claim_token_in_logs(
    client: AsyncClient,
    evidence_repository: FakeEvidenceLifecycleRepository,
    storage: FakeObjectStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _seed_claimed_job(evidence_repository, storage)
    with caplog.at_level("DEBUG"):
        await client.get(
            f"/api/v1/internal/worker-jobs/{job.job_id}/input",
            headers={"X-Claim-Token": _CLAIM_TOKEN},
        )
        # A wrong-token attempt too, so a rejection path is also captured.
        await client.get(
            f"/api/v1/internal/worker-jobs/{job.job_id}/input",
            headers={"X-Claim-Token": "wrong-token-value"},
        )
    log_text = caplog.text
    assert _CLAIM_TOKEN not in log_text
    assert "wrong-token-value" not in log_text
