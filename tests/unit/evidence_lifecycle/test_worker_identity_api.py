"""Scenarios 3, 4, 6, 7, 15, 16, 17: worker-identity HTTP-layer authentication and audits.

Unlike `test_worker_internal_api.py`/`test_worker_input_api.py` (which
override `require_worker_principal` directly for their main-flow tests),
this file exercises the *real* dependency throughout, backed by a real
`WorkerCredentialRecord` seeded into `FakeAccessControlRepository` -- the
same pattern `test_worker_internal_api.py`'s fail-closed tests already use,
just as the primary path here rather than the exception.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerStatus
from app.core.config import get_settings
from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import (
    CaseRole,
    ClearanceLevel,
    WorkerCredentialRecord,
    WorkerCredentialStatus,
)
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.access_control.worker_credentials import (
    generate_worker_token,
    hash_worker_credential,
    resolve_worker_pepper,
)
from app.modules.evidence_lifecycle.dependencies import (
    get_evidence_lifecycle_repository,
    get_job_producer,
    get_object_storage,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.models import WorkerJobRecord
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.access_control.factories import (
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_PEPPER = resolve_worker_pepper(get_settings())


@pytest.fixture
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def evidence_repository() -> FakeEvidenceLifecycleRepository:
    return FakeEvidenceLifecycleRepository()


@pytest.fixture
def _override_dependencies(
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: ac_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_evidence_lifecycle_repository] = lambda: evidence_repository
    app.dependency_overrides[get_object_storage] = lambda: FakeObjectStorage()
    app.dependency_overrides[get_job_producer] = FakeJobProducer
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _provision_worker(
    ac_repository: FakeAccessControlRepository,
    *,
    allowed_processor_names: tuple[str, ...] = ("fir_report_text_v1",),
    status: WorkerCredentialStatus = WorkerCredentialStatus.ACTIVE,
) -> tuple[UUID, str]:
    """Seed a real worker credential; returns `(worker_id, plaintext_token)`."""
    token = generate_worker_token()
    worker_id = uuid4()
    record = WorkerCredentialRecord(
        worker_id=worker_id,
        display_name="test-worker",
        status=status,
        allowed_processor_names=allowed_processor_names,
        credential_digest=hash_worker_credential(token, _PEPPER),
        created_at=FIXED_TIME,
        rotated_at=None,
        revoked_at=None,
    )
    await ac_repository.create_worker_credential(record)
    return worker_id, token


def _seed_queued_job(
    repository: FakeEvidenceLifecycleRepository,
    *,
    processor_name: str = "fir_report_text_v1",
    processor_version: str = "1.0.0",
) -> WorkerJobRecord:
    case_id, evidence_id = uuid4(), uuid4()
    job = WorkerJobRecord(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=SourceType.DOCUMENT,
        processor_name=processor_name,
        processor_version=processor_version,
        attempt=1,
        max_attempts=5,
        idempotency_key=f"{case_id}:{evidence_id}:{processor_name}:{processor_version}",
        input_object_uri=f"cases/{case_id}/evidence/{evidence_id}/original",
        requested_at=FIXED_TIME,
        status=WorkerStatus.QUEUED,
        queued_at=FIXED_TIME,
        dispatched_at=None,
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
    return job


# --- Scenario 3: a valid worker credential authenticates successfully -------


async def test_valid_worker_credential_authenticates_and_claims(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    worker_id, token = await _provision_worker(ac_repository)
    job = _seed_queued_job(evidence_repository)

    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": job.processor_name, "processor_version": job.processor_version},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["job_id"] == str(job.job_id)
    assert evidence_repository.jobs[job.job_id].claimed_by_worker_id == worker_id


async def test_successful_authentication_populates_the_heartbeat_registry(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    """Gap-Closure WP-6 (G16): `require_worker_principal` touches `last_seen_at`
    on every successful auth -- no separate heartbeat endpoint is needed to
    populate the registry `GET /api/v1/admin/workers` reads from."""
    worker_id, token = await _provision_worker(ac_repository)
    assert ac_repository.worker_credentials[worker_id].last_seen_at is None
    job = _seed_queued_job(evidence_repository)

    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": job.processor_name, "processor_version": job.processor_version},
    )

    assert response.status_code == 200, response.text
    assert ac_repository.worker_credentials[worker_id].last_seen_at is not None


# --- Gap-Closure WP-6 (G16, re-close): GET /api/v1/internal/workers ---------


async def test_worker_fleet_route_is_reachable_with_any_valid_worker_credential(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    """The plan requires this route to be worker-credential-scoped, not
    admin-only -- any authenticated worker can see fleet health."""
    _worker_id, token = await _provision_worker(ac_repository)

    response = await client.get(
        "/api/v1/internal/workers", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_worker_fleet_route_requires_worker_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/internal/workers")
    assert response.status_code == 401


async def test_worker_fleet_route_never_exposes_a_credential_digest(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    _worker_id, token = await _provision_worker(ac_repository)
    response = await client.get(
        "/api/v1/internal/workers", headers={"Authorization": f"Bearer {token}"}
    )
    assert "credential_digest" not in response.text


# --- Scenario 4: missing/malformed/blank/invalid/revoked credentials fail closed ---


async def test_missing_authorization_header_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header_value",
    [
        "NotBearer sometoken",  # malformed scheme
        "Bearer ",  # blank token
        "Bearer    ",  # whitespace-only token
    ],
)
async def test_malformed_or_blank_authorization_header_is_401(
    client: AsyncClient, header_value: str
) -> None:
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": header_value},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 401


async def test_unknown_token_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": "Bearer this-token-was-never-issued"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 401


async def test_revoked_credential_is_401(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    _worker_id, token = await _provision_worker(
        ac_repository, status=WorkerCredentialStatus.REVOKED
    )
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 401


async def test_credential_revoked_after_issuance_immediately_denies(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    """A previously-valid token stops working the moment its credential is revoked."""
    worker_id, token = await _provision_worker(ac_repository)
    first = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert first.status_code == 200

    await ac_repository.revoke_worker_credential(worker_id, FIXED_TIME)

    second = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert second.status_code == 401


# --- Scenarios 6-7: processor scoping, allowed and denied -------------------


async def test_worker_can_claim_an_allowed_processor(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    _worker_id, token = await _provision_worker(
        ac_repository, allowed_processor_names=("cdr_generic_v1",)
    )
    job = _seed_queued_job(evidence_repository, processor_name="cdr_generic_v1")

    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 200
    assert response.json()["job"]["job_id"] == str(job.job_id)


async def test_claim_records_a_worker_job_claimed_audit_event(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    worker_id, token = await _provision_worker(
        ac_repository, allowed_processor_names=("cdr_generic_v1",)
    )
    job = _seed_queued_job(evidence_repository, processor_name="cdr_generic_v1")

    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 200

    events = [e for e in ac_repository.audit_events if e.event_type == "worker_job_claimed"]
    assert len(events) == 1
    assert events[0].metadata_safe_json["job_id"] == str(job.job_id)
    assert events[0].metadata_safe_json["worker_id"] == str(worker_id)
    assert events[0].metadata_safe_json["attempt"] == 1
    assert events[0].case_id_nullable == job.case_id
    assert not any(e.event_type == "worker_job_reclaimed" for e in ac_repository.audit_events)


async def test_reclaim_records_a_worker_job_reclaimed_audit_event(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    _worker_id, token = await _provision_worker(
        ac_repository, allowed_processor_names=("cdr_generic_v1",)
    )
    job = _seed_queued_job(evidence_repository, processor_name="cdr_generic_v1")
    # Simulate a prior claim whose lease has already expired.
    evidence_repository.jobs[job.job_id] = job.model_copy(
        update={
            "status": WorkerStatus.RUNNING,
            "claimed_at": FIXED_TIME,
            "lease_expires_at": datetime(2020, 1, 1, tzinfo=UTC),
            "claim_token_hash": "stale-hash-from-a-previous-worker",
            "claimed_by_worker_id": uuid4(),
        }
    )

    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 200
    assert response.json()["job"]["attempt"] == 2

    events = [e for e in ac_repository.audit_events if e.event_type == "worker_job_reclaimed"]
    assert len(events) == 1
    assert events[0].metadata_safe_json["job_id"] == str(job.job_id)
    assert events[0].metadata_safe_json["attempt"] == 2
    assert not any(e.event_type == "worker_job_claimed" for e in ac_repository.audit_events)


async def test_worker_with_no_scope_for_a_processor_is_denied_and_audited(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    worker_id, token = await _provision_worker(
        ac_repository, allowed_processor_names=("cdr_generic_v1",)
    )
    _seed_queued_job(evidence_repository, processor_name="fir_report_text_v1")

    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )

    assert response.status_code == 403
    denial_events = [
        e for e in ac_repository.audit_events if e.event_type == "worker_processor_scope_denied"
    ]
    assert len(denial_events) == 1
    assert denial_events[0].metadata_safe_json["worker_id"] == str(worker_id)
    assert denial_events[0].metadata_safe_json["processor_name"] == "fir_report_text_v1"


# --- Scenarios 15-16: denied worker actions are audited, safely ------------


async def test_worker_authentication_denial_is_audited_without_secrets(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    response = await client.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": "Bearer some-unknown-token-value"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert response.status_code == 401

    denial_events = [
        e for e in ac_repository.audit_events if e.event_type == "worker_authentication_denied"
    ]
    assert len(denial_events) == 1
    metadata_text = str(denial_events[0].metadata_safe_json)
    assert "some-unknown-token-value" not in metadata_text
    assert "Bearer" not in metadata_text


async def test_worker_job_access_denial_is_audited_without_claim_token_or_uri(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    _worker_id, token = await _provision_worker(ac_repository)
    job = _seed_queued_job(evidence_repository)

    response = await client.post(
        f"/api/v1/internal/worker-jobs/{job.job_id}/result",
        headers={"Authorization": f"Bearer {token}", "X-Claim-Token": "totally-wrong-token"},
        json={
            "schema_version": "v1",
            "job_id": str(job.job_id),
            "case_id": str(job.case_id),
            "evidence_id": str(job.evidence_id),
            "status": "succeeded",
            "observations": [],
            "derived_artifacts": [],
            "checkpoint": None,
            "error": None,
            "completed_at": FIXED_TIME.isoformat(),
        },
    )
    assert response.status_code == 401

    denial_events = [
        e for e in ac_repository.audit_events if e.event_type == "worker_job_access_denied"
    ]
    assert len(denial_events) == 1
    metadata = denial_events[0].metadata_safe_json
    assert metadata["job_id"] == str(job.job_id)
    assert "reason" in metadata
    metadata_text = str(metadata)
    assert "totally-wrong-token" not in metadata_text
    assert job.input_object_uri not in metadata_text


async def test_no_audit_event_ever_contains_a_bearer_token_claim_token_or_object_uri(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    """A broad sweep across several denial paths, checked once at the end."""
    worker_id, token = await _provision_worker(
        ac_repository, allowed_processor_names=("cdr_generic_v1",)
    )
    job = _seed_queued_job(evidence_repository, processor_name="fir_report_text_v1")

    await client.post(  # scope denial
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {token}"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    await client.get(  # job-access denial (wrong token)
        f"/api/v1/internal/worker-jobs/{job.job_id}/input",
        headers={"Authorization": f"Bearer {token}", "X-Claim-Token": "wrong-token-value"},
    )
    await client.post(  # authentication denial
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": "Bearer unknown-token-value"},
        json={"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"},
    )

    assert len(ac_repository.audit_events) >= 3
    for event in ac_repository.audit_events:
        blob = str(event.model_dump())
        assert token not in blob
        assert "wrong-token-value" not in blob
        assert "unknown-token-value" not in blob
        assert job.input_object_uri not in blob


# --- Scenario 17: user-facing job status never reveals worker identity internals ---


async def test_job_view_never_exposes_worker_identity_internals(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    email = f"agent-{uuid4().hex[:10]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105
    user = make_user_record(email_normalized=email)
    await ac_repository.create_user(user)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    user_token = login.json()["access_token"]
    user_id = user.user_id

    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await ac_repository.create_case(case)
    membership = make_membership_record(
        case_id=case.case_id,
        user_id=user_id,
        role=CaseRole.INVESTIGATOR,
        clearance=ClearanceLevel.CONFIDENTIAL,
    )
    await ac_repository.create_membership(membership)

    worker_id, _worker_token = await _provision_worker(ac_repository)
    job = _seed_queued_job(evidence_repository)
    evidence_repository.jobs[job.job_id] = job.model_copy(
        update={
            "case_id": case.case_id,
            "status": WorkerStatus.RUNNING,
            "claimed_at": FIXED_TIME,
            "lease_expires_at": FIXED_TIME,
            "claimed_by": job.processor_name,
            "claimed_by_worker_id": worker_id,
            "claim_token_hash": "a" * 64,
        }
    )

    response = await client.get(
        f"/api/v1/cases/{case.case_id}/jobs/{job.job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert response.status_code == 200
    assert "claimed_by_worker_id" not in response.text
    assert str(worker_id) not in response.text
    assert "claim_token" not in response.text
    assert "credential_digest" not in response.text
