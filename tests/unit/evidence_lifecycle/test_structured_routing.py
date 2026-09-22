"""Phase 2.3: `structured_tabular`/`structured_json` upload routing.

Mirrors `test_upload_service.py`'s service-level pattern (scenarios 1-6, 10)
and `test_evidence_api.py`'s HTTP-layer pattern (scenarios 8, 11) exactly --
no shared conftest, matching this module's existing per-file fixture
convention.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.contracts.evidence import EvidenceClassification, SourceType
from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import CaseRole, ClearanceLevel
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.evidence_lifecycle.dependencies import (
    get_evidence_lifecycle_repository,
    get_job_producer,
    get_object_storage,
)
from app.modules.evidence_lifecycle.errors import UnsupportedContentTypeError
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.access_control.factories import (
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.structured_processing.builders import build_xlsx

CSV_BYTES = b"name,value\nfoo,1\nbar,2\n"
JSON_BYTES = json.dumps({"records": [{"a": 1}, {"a": 2}]}).encode()
XLSX_BYTES = build_xlsx(["name", "value"], [["foo", 1], ["bar", 2]])
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024


# --- Service-level: scenarios 1-6, 10 ----------------------------------------


def _service() -> tuple[
    EvidenceLifecycleService, FakeEvidenceLifecycleRepository, FakeObjectStorage, FakeJobProducer
]:
    repository = FakeEvidenceLifecycleRepository()
    storage = FakeObjectStorage()
    job_producer = FakeJobProducer()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=storage,
        job_producer=job_producer,
        max_evidence_bytes=DEFAULT_MAX_BYTES,
    )
    return service, repository, storage, job_producer


def _context() -> UploadContext:
    return UploadContext(now=datetime.now(UTC), request_id="req-test")


async def test_valid_csv_upload_as_structured_tabular_creates_generic_tabular_job() -> None:
    service, repository, storage, _ = _service()
    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.STRUCTURED_TABULAR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=CSV_BYTES, filename="data.csv", content_type="text/csv"),
        idempotency_key=None,
        context=_context(),
    )
    assert outcome.job.processor_name == "generic_tabular_v1"
    assert outcome.job.processor_version == "1.0.0"
    assert outcome.job.source_type is SourceType.STRUCTURED_TABULAR
    assert storage.objects[outcome.evidence.object_uri] == CSV_BYTES


async def test_valid_xlsx_upload_as_structured_tabular_creates_generic_tabular_job() -> None:
    service, *_ = _service()
    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.STRUCTURED_TABULAR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(
            content=XLSX_BYTES, filename="data.xlsx", content_type=XLSX_CONTENT_TYPE
        ),
        idempotency_key=None,
        context=_context(),
    )
    assert outcome.job.processor_name == "generic_tabular_v1"


async def test_valid_json_upload_as_structured_json_creates_generic_json_job() -> None:
    service, *_ = _service()
    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.STRUCTURED_JSON,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(
            content=JSON_BYTES, filename="data.json", content_type="application/json"
        ),
        idempotency_key=None,
        context=_context(),
    )
    assert outcome.job.processor_name == "generic_json_v1"
    assert outcome.job.processor_version == "1.0.0"
    assert outcome.job.source_type is SourceType.STRUCTURED_JSON


async def test_structured_tabular_with_json_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.STRUCTURED_TABULAR,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=JSON_BYTES, filename="data.json", content_type="application/json"
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}
    assert repository.evidence == {}
    assert repository.jobs == {}


async def test_structured_json_with_csv_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.STRUCTURED_JSON,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=CSV_BYTES, filename="data.csv", content_type="text/csv"
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}
    assert repository.evidence == {}


async def test_structured_json_with_xlsx_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.STRUCTURED_JSON,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=XLSX_BYTES, filename="data.xlsx", content_type=XLSX_CONTENT_TYPE
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}


async def test_structured_tabular_rejects_unsupported_mime_type() -> None:
    service, *_ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.STRUCTURED_TABULAR,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content=b"plain text", content_type="text/plain"),
            idempotency_key=None,
            context=_context(),
        )


async def test_structured_json_idempotent_replay_returns_original() -> None:
    service, repository, storage, job_producer = _service()
    kwargs = {
        "case_id": uuid4(),
        "uploaded_by": uuid4(),
        "source_type": SourceType.STRUCTURED_JSON,
        "classification": EvidenceClassification.UNCLASSIFIED,
        "parser_profile": None,
        "idempotency_key": "structured-json-retry-1",
    }
    upload_kwargs = {
        "content_type": "application/json",
        "filename": "data.json",
    }

    first = await service.upload_evidence(
        upload=make_upload_file(content=JSON_BYTES, **upload_kwargs), context=_context(), **kwargs
    )
    second = await service.upload_evidence(
        upload=make_upload_file(content=JSON_BYTES, **upload_kwargs), context=_context(), **kwargs
    )

    assert first.created is True
    assert second.created is False
    assert first.evidence.evidence_id == second.evidence.evidence_id
    assert first.job.job_id == second.job.job_id
    assert len(storage.objects) == 1
    assert len(job_producer.published) == 1


async def test_structured_tabular_idempotent_replay_returns_original() -> None:
    service, repository, storage, job_producer = _service()
    kwargs = {
        "case_id": uuid4(),
        "uploaded_by": uuid4(),
        "source_type": SourceType.STRUCTURED_TABULAR,
        "classification": EvidenceClassification.UNCLASSIFIED,
        "parser_profile": None,
        "idempotency_key": "structured-tabular-retry-1",
    }
    upload_kwargs = {"content_type": "text/csv", "filename": "data.csv"}

    first = await service.upload_evidence(
        upload=make_upload_file(content=CSV_BYTES, **upload_kwargs), context=_context(), **kwargs
    )
    second = await service.upload_evidence(
        upload=make_upload_file(content=CSV_BYTES, **upload_kwargs), context=_context(), **kwargs
    )

    assert first.created is True
    assert second.created is False
    assert first.evidence.evidence_id == second.evidence.evidence_id
    assert first.job.job_id == second.job.job_id


# --- HTTP-layer: scenarios 8, 11 ---------------------------------------------


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
    app.dependency_overrides[get_object_storage] = FakeObjectStorage
    app.dependency_overrides[get_job_producer] = FakeJobProducer
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _authenticated_member(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> tuple[str, object]:
    email = f"agent-{uuid4().hex[:10]}@example.test"
    user = make_user_record(email_normalized=email)
    await ac_repository.create_user(user)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
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
    return token, case.case_id


async def test_client_cannot_override_routed_processor_or_parser_profile(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("data.csv", CSV_BYTES, "text/csv")},
        data={
            "source_type": "structured_tabular",
            "classification": "unclassified",
            # An attempted override -- must be silently ignored, never honored.
            "parser_profile": "attacker_supplied_profile_v99",
        },
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()

    assert body["evidence"]["parser_profile"] == "generic_tabular_v1"
    assert body["job"]["processor_name"] == "generic_tabular_v1"
    assert body["job"]["processor_version"] == "1.0.0"


async def test_case_a_member_cannot_read_case_b_structured_evidence(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token_a, case_a = await _authenticated_member(client, ac_repository)
    token_b, case_b = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_b}/evidence",
        headers={"Authorization": f"Bearer {token_b}"},
        files={"file": ("data.json", JSON_BYTES, "application/json")},
        data={"source_type": "structured_json", "classification": "unclassified"},
    )
    assert upload.status_code == 201
    evidence_id = upload.json()["evidence"]["evidence_id"]

    cross_case = await client.get(
        f"/api/v1/cases/{case_b}/evidence/{evidence_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert cross_case.status_code == 403

    own_list = await client.get(
        f"/api/v1/cases/{case_a}/evidence", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert own_list.status_code == 200
    assert own_list.json()["items"] == []
