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
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
from app.modules.evidence_lifecycle.models import (
    ObservationBatchRecord,
    ObservationRecord,
    WorkerProgressEventRecord,
    WorkerResultRecord,
)
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from app.modules.integrity.dependencies import get_integrity_service
from tests.fixtures.access_control.factories import (
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

VALID_PASSWORD = "correct-horse-battery-staple"
assert len(VALID_PASSWORD) >= MIN_PASSWORD_LENGTH


class _NoopIntegrityService:
    """Keeps evidence HTTP tests fully in-memory instead of opening a real DB."""

    async def record_integrity_event(self, *args: object, **kwargs: object) -> None:
        return None


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
    app.dependency_overrides[get_integrity_service] = _NoopIntegrityService
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
    """Seed+log in a real user (no public self-registration exists -- G5), then
    seed a case + active membership for them.
    """
    email = f"agent-{uuid4().hex[:10]}@example.test"
    user = make_user_record(email_normalized=email)
    await ac_repository.create_user(user)
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    user_id = user.user_id

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


def _pdf_upload_files() -> dict:
    return {"file": ("case-source.pdf", b"%PDF-1.7\nsynthetic PDF", "application/pdf")}


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


async def test_upload_detects_bytes_and_inherits_case_classification(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    """The normal upload body cannot pick a type or lower classification."""
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_pdf_upload_files(),
        # Old clients may still send a type. It is deliberately ignored.
        data={"source_type": "financial"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["evidence"]["source_type"] == "document"
    assert body["evidence"]["content_type"] == "application/pdf"
    assert body["evidence"]["classification"] == "confidential"
    assert body["job"]["processor_name"] == "fir_report_text_v1"


async def test_library_and_content_are_case_scoped_and_use_safe_inline_source_delivery(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token_a, case_a = await _authenticated_member(client, ac_repository)
    token_b, case_b = await _authenticated_member(client, ac_repository)
    upload = await client.post(
        f"/api/v1/cases/{case_b}/evidence",
        headers={"Authorization": f"Bearer {token_b}"},
        files=_pdf_upload_files(),
    )
    assert upload.status_code == 201, upload.text
    evidence_id = upload.json()["evidence"]["evidence_id"]

    own_library = await client.get(
        f"/api/v1/cases/{case_b}/evidence/library?query=case-source",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert own_library.status_code == 200, own_library.text
    assert [item["evidence"]["evidence_id"] for item in own_library.json()["items"]] == [
        evidence_id
    ]

    cross_case = await client.get(
        f"/api/v1/cases/{case_b}/evidence/{evidence_id}/content",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert cross_case.status_code == 403
    assert str(case_a) not in cross_case.text

    source = await client.get(
        f"/api/v1/cases/{case_b}/evidence/{evidence_id}/content",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert source.status_code == 200
    assert source.content.startswith(b"%PDF-")
    assert source.headers["content-type"].startswith("application/pdf")
    assert source.headers["content-disposition"].startswith("inline;")
    assert "minio" not in source.text.lower()


async def test_case_owner_cannot_lower_evidence_below_case_classification(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository, role=CaseRole.CASE_OWNER)
    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
    )
    evidence_id = upload.json()["evidence"]["evidence_id"]
    response = await client.patch(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/classification",
        headers={"Authorization": f"Bearer {token}"},
        json={"classification": "restricted"},
    )
    assert response.status_code == 409


async def test_only_case_owner_can_raise_evidence_classification_during_upload(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    owner_token, owner_case_id = await _authenticated_member(
        client, ac_repository, role=CaseRole.CASE_OWNER
    )
    raised = await client.post(
        f"/api/v1/cases/{owner_case_id}/evidence",
        headers={"Authorization": f"Bearer {owner_token}"},
        files=_upload_files(),
        data={"classification": "secret"},
    )
    assert raised.status_code == 201, raised.text
    assert raised.json()["evidence"]["classification"] == "secret"

    member_token, member_case_id = await _authenticated_member(client, ac_repository)
    denied = await client.post(
        f"/api/v1/cases/{member_case_id}/evidence",
        headers={"Authorization": f"Bearer {member_token}"},
        files=_upload_files(),
        data={"classification": "secret"},
    )
    assert denied.status_code == 403


async def test_highly_sensitive_evidence_is_hidden_from_lower_clearance_case_members(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    owner_token, case_id = await _authenticated_member(
        client, ac_repository, role=CaseRole.CASE_OWNER
    )
    member_email = f"confidential-member-{uuid4().hex[:10]}@example.test"
    member = make_user_record(email_normalized=member_email)
    await ac_repository.create_user(member)
    await ac_repository.create_membership(
        make_membership_record(
            case_id=case_id,
            user_id=member.user_id,
            role=CaseRole.INVESTIGATOR,
            clearance=ClearanceLevel.CONFIDENTIAL,
        )
    )
    member_login = await client.post(
        "/api/v1/auth/login", json={"email": member_email, "password": VALID_PASSWORD}
    )
    assert member_login.status_code == 200
    member_headers = {"Authorization": f"Bearer {member_login.json()['access_token']}"}

    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {owner_token}"},
        files=_upload_files(filename="restricted-to-secret.txt"),
        data={"classification": "secret"},
    )
    assert upload.status_code == 201, upload.text
    evidence_id = upload.json()["evidence"]["evidence_id"]

    listing = await client.get(f"/api/v1/cases/{case_id}/evidence/library", headers=member_headers)
    assert listing.status_code == 200
    assert listing.json()["items"] == []
    stream = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/content", headers=member_headers
    )
    assert stream.status_code == 403
    assert evidence_id not in stream.text


# --- Scenario 13: user-facing job status is case-scoped and leaks nothing ---


async def test_completed_job_status_exposes_safe_fields_only(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    """Simulates a worker having claimed + completed the job, then checks the user-facing view."""
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}
    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    job_id = upload.json()["job"]["job_id"]
    evidence_id = upload.json()["evidence"]["evidence_id"]
    now = datetime.now(UTC)

    job = evidence_repository.jobs[UUID(job_id)]
    evidence_repository.jobs[UUID(job_id)] = job.model_copy(
        update={
            "status": "succeeded",
            "claimed_at": now,
            "lease_expires_at": now,
            "claimed_by": "fir_report_text_v1",
            "claim_token_hash": "a" * 64,
        }
    )
    result_id = uuid4()
    observation_id = uuid4()
    evidence_repository.results[result_id] = WorkerResultRecord(
        result_id=result_id,
        job_id=UUID(job_id),
        case_id=case_id,
        evidence_id=UUID(evidence_id),
        attempt=1,
        status="succeeded",
        derived_artifacts=[],
        checkpoint=None,
        error_code=None,
        error_message=None,
        error_retryable=None,
        canonical_payload={},
        payload_hash="b" * 64,
        completed_at=now,
        created_at=now,
        updated_at=now,
    )
    evidence_repository.observations[observation_id] = ObservationRecord(
        observation_id=observation_id,
        result_id=result_id,
        observation_batch_id=None,
        job_id=UUID(job_id),
        case_id=case_id,
        evidence_id=UUID(evidence_id),
        observation_type="document_text_mention",
        canonical_payload={},
        created_at=now,
    )

    response = await client.get(f"/api/v1/cases/{case_id}/jobs/{job_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["observation_count"] == 1
    assert body["completed_at"] is not None
    assert body["claimed_at"] is not None
    # Never leaked, regardless of what's stored internally.
    assert "claim_token" not in response.text
    assert "claim_token_hash" not in response.text
    assert "object_uri" not in response.text
    assert "a" * 64 not in response.text  # the claim_token_hash value itself


# --- Scenario 23: job progress is case-scoped and contains only safe fields --


async def test_job_status_exposes_latest_progress_summary_scoped_and_safe(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    """Simulates a worker having submitted a partial observation batch (Phase 3)."""
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}
    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(),
        data={"source_type": "document", "classification": "unclassified"},
    )
    job_id = UUID(upload.json()["job"]["job_id"])
    evidence_id = UUID(upload.json()["evidence"]["evidence_id"])
    now = datetime.now(UTC)

    observation_batch_id = uuid4()
    evidence_repository.observation_batches[observation_batch_id] = ObservationBatchRecord(
        observation_batch_id=observation_batch_id,
        job_id=job_id,
        case_id=case_id,
        evidence_id=evidence_id,
        batch_id="batch-1",
        batch_sequence=0,
        idempotency_key="idem-1",
        is_final_batch=False,
        observation_count=3,
        payload_hash="c" * 64,
        submitted_at=now,
        created_at=now,
    )
    progress_id = uuid4()
    evidence_repository.progress_events[progress_id] = WorkerProgressEventRecord(
        progress_event_id=progress_id,
        ordinal=1,
        observation_batch_id=observation_batch_id,
        job_id=job_id,
        case_id=case_id,
        evidence_id=evidence_id,
        attempt=1,
        stage="parsing",
        units_total=10,
        units_completed=3,
        observations_emitted=3,
        batch_sequence=0,
        message_code="PAGE_PARSED",
        occurred_at=now,
        created_at=now,
    )

    response = await client.get(f"/api/v1/cases/{case_id}/jobs/{job_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["latest_progress"]["stage"] == "parsing"
    assert body["latest_progress"]["units_completed"] == 3
    assert body["latest_progress"]["message_code"] == "PAGE_PARSED"
    # Never leaked, regardless of what's stored internally.
    assert "object_uri" not in response.text
    assert "claim_token" not in response.text


async def test_job_progress_lookup_for_an_unknown_job_is_a_plain_404(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    """A job with no progress events (or no job at all) never crashes or leaks -- a plain 404.

    Exercises the same `get_job` route now also calling `get_job_progress_summary`
    -- a job that doesn't exist for this case must still fail exactly as before.
    """
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.get(f"/api/v1/cases/{case_id}/jobs/{uuid4()}", headers=headers)
    assert response.status_code == 404


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
        files={"file": ("unsupported.bin", b"\x00\xff\x00\xfe", "video/mp4")},
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
    await ac_repository.create_user(make_user_record(email_normalized=email))
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
    await ac_repository.create_user(make_user_record(email_normalized=email))
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


async def test_evidence_above_member_clearance_is_hidden_and_denied(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    """G6 per-evidence classification hook: a CONFIDENTIAL-clearance member
    can see an `unclassified` item but not a `secret`-classified one -- 403
    on direct read, and silently absent from the list (never a leaked
    existence signal).
    """
    token, case_id = await _authenticated_member(client, ac_repository, role=CaseRole.INVESTIGATOR)
    headers = {"Authorization": f"Bearer {token}"}

    visible_upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(filename="visible.txt"),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert visible_upload.status_code == 201
    visible_id = visible_upload.json()["evidence"]["evidence_id"]

    hidden_upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers=headers,
        files=_upload_files(filename="hidden.txt"),
    )
    assert hidden_upload.status_code == 201
    hidden_id = hidden_upload.json()["evidence"]["evidence_id"]
    # An owner-only classification increase is tested separately.  Seed the
    # resulting protected state here to exercise the read/list ABAC boundary.
    hidden_record = evidence_repository.evidence[UUID(hidden_id)]
    evidence_repository.evidence[UUID(hidden_id)] = hidden_record.model_copy(
        update={"classification": "secret"}
    )

    get_hidden = await client.get(f"/api/v1/cases/{case_id}/evidence/{hidden_id}", headers=headers)
    assert get_hidden.status_code == 403

    get_visible = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{visible_id}", headers=headers
    )
    assert get_visible.status_code == 200

    listing = await client.get(f"/api/v1/cases/{case_id}/evidence", headers=headers)
    assert listing.status_code == 200
    listed_ids = {item["evidence_id"] for item in listing.json()["items"]}
    assert visible_id in listed_ids
    assert hidden_id not in listed_ids


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


# --- Gap-Closure WP-4 (G7): evidence integrity check + reprocess -------------


async def _upload(
    client: AsyncClient, case_id: object, token: str, *, content: bytes = b"FIR No. 1/2026"
) -> str:
    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(content=content),
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert upload.status_code == 201, upload.text
    return str(upload.json()["evidence"]["evidence_id"])


async def test_integrity_check_matches_for_untampered_object(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    evidence_id = await _upload(client, case_id, token)

    response = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/integrity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matches"] is True
    assert body["stored_sha256"] == body["recomputed_sha256"]


async def test_integrity_check_flags_a_tampered_object(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
    object_storage: FakeObjectStorage,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    evidence_id = await _upload(client, case_id, token)

    record = evidence_repository.evidence[UUID(evidence_id)]
    object_storage.objects[record.object_uri] = b"tampered bytes"

    response = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/integrity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["matches"] is False
    assert body["stored_sha256"] != body["recomputed_sha256"]


async def test_integrity_check_is_503_when_object_is_unreadable(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
    object_storage: FakeObjectStorage,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    evidence_id = await _upload(client, case_id, token)

    record = evidence_repository.evidence[UUID(evidence_id)]
    del object_storage.objects[record.object_uri]

    response = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/integrity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503


async def test_integrity_check_is_403_above_member_clearance(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    evidence_repository: FakeEvidenceLifecycleRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload_files(),
    )
    evidence_id = upload.json()["evidence"]["evidence_id"]
    protected_record = evidence_repository.evidence[UUID(evidence_id)]
    evidence_repository.evidence[UUID(evidence_id)] = protected_record.model_copy(
        update={"classification": "secret"}
    )

    response = await client.get(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/integrity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_reprocess_enqueues_a_distinct_job_from_the_original_upload(
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
    evidence_id = upload.json()["evidence"]["evidence_id"]
    original_job_id = upload.json()["job"]["job_id"]

    response = await client.post(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/reprocess",
        headers={**headers, "Idempotency-Key": "retry-1"},
    )
    assert response.status_code == 201, response.text
    new_job_id = response.json()["job"]["job_id"]
    assert new_job_id != original_job_id


async def test_reprocess_replays_on_the_same_idempotency_key(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    headers = {"Authorization": f"Bearer {token}"}
    evidence_id = await _upload(client, case_id, token)

    first = await client.post(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/reprocess",
        headers={**headers, "Idempotency-Key": "retry-1"},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/reprocess",
        headers={**headers, "Idempotency-Key": "retry-1"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["job"]["job_id"] == first.json()["job"]["job_id"]


async def test_reprocess_requires_evidence_write_and_is_case_scoped(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository, role=CaseRole.VIEWER)
    other_token, other_case_id = await _authenticated_member(client, ac_repository)
    evidence_id = await _upload(client, other_case_id, other_token)

    denied_by_role = await client.post(
        f"/api/v1/cases/{other_case_id}/evidence/{evidence_id}/reprocess",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "retry-1"},
    )
    assert denied_by_role.status_code == 403

    denied_cross_case = await client.post(
        f"/api/v1/cases/{case_id}/evidence/{evidence_id}/reprocess",
        headers={"Authorization": f"Bearer {other_token}", "Idempotency-Key": "retry-1"},
    )
    assert denied_cross_case.status_code == 403


async def test_reprocess_unknown_evidence_is_404(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.post(
        f"/api/v1/cases/{case_id}/evidence/{uuid4()}/reprocess",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "retry-1"},
    )
    assert response.status_code == 404
