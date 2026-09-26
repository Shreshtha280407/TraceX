"""Phase 2 routing fix: `audio_transcript`/`audio_diarization`/`whatsapp_chat`/
`telegram_chat`/`instagram_chat` upload routing.

Mirrors `test_structured_routing.py`'s pattern exactly (service-level +
HTTP-layer, no shared conftest) for the five new, additive `SourceType`
values that make `communication_processing`'s previously upload-unreachable
profiles (`transcript_import_v1`, `diarization_import_v1`,
`whatsapp_export_v1`, `telegram_export_v1`, `instagram_export_v1`) real,
server-routable source types for the first time -- see
`docs/architecture/phase-2-decisions.md`.
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
from app.modules.integrity.dependencies import get_integrity_service
from tests.fixtures.access_control.factories import (
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

JSON_BYTES = json.dumps({"segments": []}).encode()
WHATSAPP_BYTES = b"01/01/26, 12:00 - Alice: hello\n"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024


class _NoopIntegrityService:
    async def record_integrity_event(self, *args: object, **kwargs: object) -> None:
        return None


_VALID_ROUTES = [
    pytest.param(
        SourceType.AUDIO_TRANSCRIPT,
        "application/json",
        JSON_BYTES,
        "segments.json",
        "transcript_import_v1",
        id="audio_transcript",
    ),
    pytest.param(
        SourceType.AUDIO_DIARIZATION,
        "application/json",
        JSON_BYTES,
        "segments.json",
        "diarization_import_v1",
        id="audio_diarization",
    ),
    pytest.param(
        SourceType.WHATSAPP_CHAT,
        "text/plain",
        WHATSAPP_BYTES,
        "chat.txt",
        "whatsapp_export_v1",
        id="whatsapp_chat",
    ),
    pytest.param(
        SourceType.TELEGRAM_CHAT,
        "application/json",
        JSON_BYTES,
        "export.json",
        "telegram_export_v1",
        id="telegram_chat",
    ),
    pytest.param(
        SourceType.INSTAGRAM_CHAT,
        "application/json",
        JSON_BYTES,
        "export.json",
        "instagram_export_v1",
        id="instagram_chat",
    ),
]


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


# --- Service-level: each new source type routes to its real processor ------


@pytest.mark.parametrize(
    ("source_type", "content_type", "content", "filename", "processor"), _VALID_ROUTES
)
async def test_valid_upload_routes_to_the_real_communication_processor(
    source_type: SourceType, content_type: str, content: bytes, filename: str, processor: str
) -> None:
    service, _repository, storage, _ = _service()
    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=source_type,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=content, filename=filename, content_type=content_type),
        idempotency_key=None,
        context=_context(),
    )
    assert outcome.job.processor_name == processor
    assert outcome.job.processor_version == "1.0.0"
    assert outcome.job.source_type is source_type
    assert outcome.evidence.parser_profile == processor
    assert storage.objects[outcome.evidence.object_uri] == content


# --- Cross-MIME rejection: each source type accepts only its one content type ---


async def test_whatsapp_chat_with_json_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.WHATSAPP_CHAT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=JSON_BYTES, filename="export.json", content_type="application/json"
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}
    assert repository.evidence == {}
    assert repository.jobs == {}


async def test_telegram_chat_with_plain_text_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.TELEGRAM_CHAT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=WHATSAPP_BYTES, filename="chat.txt", content_type="text/plain"
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}
    assert repository.evidence == {}


async def test_audio_transcript_rejects_unsupported_mime_type() -> None:
    service, *_ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.AUDIO_TRANSCRIPT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content=b"RIFF....WAVE", content_type="audio/wav"),
            idempotency_key=None,
            context=_context(),
        )


# --- Idempotency: same replay pattern as every other source type -----------


async def test_telegram_chat_idempotent_replay_returns_original() -> None:
    service, repository, storage, job_producer = _service()
    kwargs = {
        "case_id": uuid4(),
        "uploaded_by": uuid4(),
        "source_type": SourceType.TELEGRAM_CHAT,
        "classification": EvidenceClassification.UNCLASSIFIED,
        "parser_profile": None,
        "idempotency_key": "telegram-chat-retry-1",
    }
    upload_kwargs = {"content_type": "application/json", "filename": "export.json"}

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


# --- HTTP-layer: client cannot override routing, cross-case isolation ------


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
    app.dependency_overrides[get_integrity_service] = _NoopIntegrityService
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


async def test_client_cannot_override_routed_processor_for_whatsapp_chat(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("chat.txt", WHATSAPP_BYTES, "text/plain")},
        data={
            "source_type": "whatsapp_chat",
            "classification": "unclassified",
            "parser_profile": "attacker_supplied_profile_v99",
        },
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()

    assert body["evidence"]["parser_profile"] == "whatsapp_export_v1"
    assert body["job"]["processor_name"] == "whatsapp_export_v1"
    assert body["job"]["processor_version"] == "1.0.0"


async def test_case_a_member_cannot_read_case_b_communication_evidence(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token_a, case_a = await _authenticated_member(client, ac_repository)
    token_b, case_b = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_b}/evidence",
        headers={"Authorization": f"Bearer {token_b}"},
        files={"file": ("export.json", JSON_BYTES, "application/json")},
        data={"source_type": "instagram_chat", "classification": "unclassified"},
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
