"""Phase 2 completion: `image`/`video` upload routing to `media_processing`.

Mirrors `test_communication_routing.py`'s pattern exactly (service-level +
HTTP-layer, no shared conftest). `SourceType.IMAGE`/`SourceType.VIDEO`
routed to `media_metadata_v1` (metadata-only) from Phase 1 through the
Phase 2 media-worker-foundation build; the Phase 2 closeout (real local
detection/OCR/tracking) re-routed both to `media_detection_v1` instead --
see `docs/architecture/phase-2-decisions.md`'s "Real local media inference
closeout" -- since that processor's own worker always emits the identical
metadata observation first regardless of which processor claimed the job,
plus real detections/OCR/tracking on top when a detector is configured.
Also covers the real gap the media-worker-foundation build's live wiring
found: `evidence_lifecycle/routing.py` has always accepted
`video/x-matroska` for `SourceType.VIDEO`, but `media_processing`'s own
`MediaKind` had no matching entry until that phase (see
`app/modules/media_processing/source.py`) -- routing alone passing was
never sufficient proof the worker could actually process it.
"""

from __future__ import annotations

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
from tests.fixtures.access_control.factories import make_case_record, make_membership_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.media_processing.synthetic import make_png_bytes

PNG_BYTES = make_png_bytes()
FAKE_MP4_BYTES = b"fake-mp4-container-bytes-not-a-real-video"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024

_VALID_ROUTES = [
    pytest.param(
        SourceType.IMAGE,
        "image/jpeg",
        PNG_BYTES,
        "photo.jpg",
        "media_detection_v1",
        id="image_jpeg",
    ),
    pytest.param(
        SourceType.IMAGE, "image/png", PNG_BYTES, "photo.png", "media_detection_v1", id="image_png"
    ),
    pytest.param(
        SourceType.VIDEO,
        "video/mp4",
        FAKE_MP4_BYTES,
        "clip.mp4",
        "media_detection_v1",
        id="video_mp4",
    ),
    pytest.param(
        SourceType.VIDEO,
        "video/quicktime",
        FAKE_MP4_BYTES,
        "clip.mov",
        "media_detection_v1",
        id="video_quicktime",
    ),
    pytest.param(
        SourceType.VIDEO,
        "video/x-matroska",
        FAKE_MP4_BYTES,
        "clip.mkv",
        "media_detection_v1",
        id="video_matroska",
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


# --- Service-level: each media source type/MIME routes to the real processor ---


@pytest.mark.parametrize(
    ("source_type", "content_type", "content", "filename", "processor"), _VALID_ROUTES
)
async def test_valid_upload_routes_to_the_real_media_processor(
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


# --- Cross-MIME rejection: image/video each accept only their own content types ---


async def test_image_with_video_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.IMAGE,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=FAKE_MP4_BYTES, filename="clip.mp4", content_type="video/mp4"
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}
    assert repository.evidence == {}
    assert repository.jobs == {}


async def test_video_with_image_mime_is_rejected_before_storage_write() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.VIDEO,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=PNG_BYTES, filename="photo.png", content_type="image/png"
            ),
            idempotency_key=None,
            context=_context(),
        )
    assert storage.objects == {}
    assert repository.evidence == {}


async def test_video_rejects_unsupported_container_mime() -> None:
    """`video/webm` is not in `SOURCE_TYPE_CONTENT_TYPES[VIDEO]` -- rejected, not guessed."""
    service, *_ = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.VIDEO,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(
                content=FAKE_MP4_BYTES, filename="clip.webm", content_type="video/webm"
            ),
            idempotency_key=None,
            context=_context(),
        )


# --- Idempotency: same replay pattern as every other source type -----------


async def test_video_idempotent_replay_returns_original() -> None:
    service, repository, storage, job_producer = _service()
    kwargs = {
        "case_id": uuid4(),
        "uploaded_by": uuid4(),
        "source_type": SourceType.VIDEO,
        "classification": EvidenceClassification.UNCLASSIFIED,
        "parser_profile": None,
        "idempotency_key": "video-retry-1",
    }
    upload_kwargs = {"content_type": "video/mp4", "filename": "clip.mp4"}

    first = await service.upload_evidence(
        upload=make_upload_file(content=FAKE_MP4_BYTES, **upload_kwargs),
        context=_context(),
        **kwargs,
    )
    second = await service.upload_evidence(
        upload=make_upload_file(content=FAKE_MP4_BYTES, **upload_kwargs),
        context=_context(),
        **kwargs,
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
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery-staple", "display_name": "Agent"},
    )
    assert register.status_code == 201, register.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    user_id = register.json()["user_id"]

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


async def test_client_cannot_override_routed_processor_for_image(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("photo.jpg", PNG_BYTES, "image/jpeg")},
        data={
            "source_type": "image",
            "classification": "unclassified",
            "parser_profile": "attacker_supplied_profile_v99",
        },
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()

    assert body["evidence"]["parser_profile"] == "media_detection_v1"
    assert body["job"]["processor_name"] == "media_detection_v1"
    assert body["job"]["processor_version"] == "1.0.0"


async def test_client_cannot_override_routed_processor_for_video(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("clip.mkv", FAKE_MP4_BYTES, "video/x-matroska")},
        data={
            "source_type": "video",
            "classification": "unclassified",
            "parser_profile": "attacker_supplied_profile_v99",
        },
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()

    assert body["evidence"]["parser_profile"] == "media_detection_v1"
    assert body["job"]["processor_name"] == "media_detection_v1"
    assert body["job"]["processor_version"] == "1.0.0"


async def test_case_a_member_cannot_read_case_b_media_evidence(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token_a, case_a = await _authenticated_member(client, ac_repository)
    token_b, case_b = await _authenticated_member(client, ac_repository)

    upload = await client.post(
        f"/api/v1/cases/{case_b}/evidence",
        headers={"Authorization": f"Bearer {token_b}"},
        files={"file": ("photo.png", PNG_BYTES, "image/png")},
        data={"source_type": "image", "classification": "unclassified"},
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
