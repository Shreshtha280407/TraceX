"""HTTP-layer tests: endpoint wiring, case scoping, auth enforcement, safe responses.

Overrides `get_access_control_repository` (auth) and the three
evidence-lifecycle providers (repository/storage/job-producer) with
in-memory fakes, so the whole router is exercised over real ASGI HTTP
semantics without any live PostgreSQL/MinIO/Redis. A real user is
registered and logged in through the actual `/api/v1/auth` flow; the case
and membership rows a case-scoped request needs are seeded directly into
the same fake access-control repository instance the app is using.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import CaseRole, ClearanceLevel
from app.modules.access_control.password import MIN_PASSWORD_LENGTH
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.evidence_lifecycle.dependencies import (
    get_evidence_lifecycle_repository,
    get_job_producer,
    get_object_storage,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.access_control.factories import make_case_record, make_membership_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

VALID_PASSWORD = "correct-horse-battery-staple"
assert len(VALID_PASSWORD) >= MIN_PASSWORD_LENGTH


@pytest.fixture
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def evidence_repository() -> FakeEvidenceLifecycleRepository:
    return FakeEvidenceLifecycleRepository()


@pytest.fixture
def object_storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def job_producer() -> FakeJobProducer:
    return FakeJobProducer()


@pytest.fixture
def _override_dependencies(
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
    object_storage: FakeObjectStorage,
    job_producer: FakeJobProducer,
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: ac_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_evidence_lifecycle_repository] = lambda: evidence_repository
    app.dependency_overrides[get_object_storage] = lambda: object_storage
    app.dependency_overrides[get_job_producer] = lambda: job_producer
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _authenticated_member(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    *,
    role: CaseRole = CaseRole.INVESTIGATOR,
) -> tuple[str, object]:
    """Register+log in a real user, then seed a case + active membership for them."""
    email = f"agent-{uuid4().hex[:10]}@example.test"
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": VALID_PASSWORD, "display_name": "Agent"},
    )
    assert register.status_code == 201, register.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    user_id = register.json()["user_id"]

    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await ac_repository.create_case(case)
    membership = make_membership_record(
        case_id=case.case_id,
        user_id=user_id,
        role=role,
        clearance=ClearanceLevel.CONFIDENTIAL,
    )
    await ac_repository.create_membership(membership)
    return token, case.case_id


def _upload_files(content: bytes = b"FIR No. 1/2026", filename: str = "fir.txt") -> dict:
    return {"file": (filename, content, "text/plain")}


# --- End-to-end wiring: upload -> get evidence -> get job -> list ----------


async def test_full_upload_get_list_round_trip(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}

    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    evidence_id = body["evidence"]["evidence_id"]
    job_id = body["job"]["job_id"]
    assert body["evidence"]["processing_status"] == "queued"
    assert body["job"]["status"] == "queued"
    assert body["job"]["processor_name"] == "fir_report_text_v1"

    get_evidence = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}", headers=headers
    )
    assert get_evidence.status_code == 200
    assert get_evidence.json()["evidence_id"] == evidence_id

    get_job = await client.get(f"/api/v1/cases/{case_id}/jobs/{job_id}", headers=headers)
    assert get_job.status_code == 200
    assert get_job.json()["job_id"] == job_id

    listing = await client.get(f"/api/v1/cases/{case_id}/evidence", headers=headers)
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


# --- Idempotency-Key header behavior -----------------------------------------


async def test_idempotency_key_replay_returns_200_not_201(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "retry-1"}

    first = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert second.status_code == 200
    assert second.json()["evidence"]["evidence_id"] == first.json()["evidence"]["evidence_id"]


async def test_conflicting_idempotency_key_reuse_is_409(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "conflict-1"}

    first = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(content=b"original"),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(content=b"different content entirely"),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert second.status_code == 409


# --- Storage failure never leaks internals -----------------------------------


async def test_storage_failure_returns_safe_generic_error(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    object_storage: FakeObjectStorage,
) -> None:
    object_storage.fail_put = True
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert response.status_code == 500
    body = response.json()
    assert "simulated storage failure" not in response.text
    assert body["error"]["message"]
    assert "request_id" in body["error"]


# --- Validation errors --------------------------------------------------------


async def test_unsupported_content_type_is_422(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files={"file": ("video.mp4", b"not really a document", "video/mp4")},
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert response.status_code == 422


async def test_empty_file_is_422(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(content=b""),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert response.status_code == 422


# --- Authorization: no access, and cross-case isolation ----------------------


async def test_user_without_case_membership_cannot_upload(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    email = f"outsider-{uuid4().hex[:10]}@example.test"
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": VALID_PASSWORD, "display_name": "Outsider"},
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]

    response = await client.post(
        f"/api/v1/cases/{uuid4()}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert response.status_code == 403


async def test_user_without_case_membership_cannot_list_or_read(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    email = f"outsider-{uuid4().hex[:10]}@example.test"
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": VALID_PASSWORD, "display_name": "Outsider"},
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    case_id = uuid4()

    assert (
        await client.get(f"/api/v1/cases/{case_id}/evidence", headers=headers)
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/cases/{case_id}/evidence/{uuid4()}", headers=headers)
    ).status_code == 403


async def test_case_a_member_cannot_read_case_b_evidence(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token_a, case_a = await _authenticated_member(client, ac_repository)
    token_b, case_b = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_b}/evidence",
        headers={"Authorization": f"Bearer {token_b}"},
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert upload.status_code == 201
    evidence_id = upload.json()["evidence"]["evidence_id"]

    # Case A's member, even though authenticated, has no membership on case B.
    cross_case = await client.get(
        f"/api/v1/cases/{case_b}/evidence/{evidence_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert cross_case.status_code == 403

    # And even scoped to their own case, case A's list never contains case B's evidence.
    own_list = await client.get(
        f"/api/v1/cases/{case_a}/evidence", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_list.status_code == 200
    assert own_list.json()["items"] == []


# --- Safe response shape ------------------------------------------------------


async def test_evidence_response_never_exposes_object_uri_or_credentials(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert upload.status_code == 201
    text = upload.text.lower()
    assert "object_uri" not in text
    assert "secret" not in text
    assert "password" not in text
    assert "minio" not in text
