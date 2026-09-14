from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.contracts.evidence import SourceType
from app.contracts.observation_batch import BatchAcceptanceStatus
from app.modules.evidence_lifecycle.errors import (
    MediaManifestValidationError,
    MediaPublicationConflictError,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.media_orchestration import (
    ArtifactRegistration,
    ChunkBoundary,
    ChunkSpec,
    MediaChunkPublication,
    build_manifest,
    chunk_identity,
)
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_job_record
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import (
    make_observation,
    make_observation_batch_submission,
)


def test_manifest_identity_is_deterministic_and_configuration_scoped() -> None:
    common = {
        "case_id": uuid4(),
        "evidence_id": uuid4(),
        "job_id": uuid4(),
        "source_type": "video",
        "processor_name": "media",
        "processor_version": "1",
        "input_object_uri": "evidence/a",
        "configuration_hash": "a",
        "chunks": (ChunkSpec(index=0, boundary=ChunkBoundary(time_start_ms=0, time_end_ms=1)),),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    first, second = build_manifest(**common), build_manifest(**common)
    assert first.manifest_id == second.manifest_id
    assert chunk_identity(first, 0) == chunk_identity(second, 0)
    assert build_manifest(**(common | {"configuration_hash": "b"})).manifest_id != first.manifest_id


def test_invalid_boundary_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ChunkBoundary(frame_start=2, frame_end=1)


def _service() -> tuple[EvidenceLifecycleService, FakeEvidenceLifecycleRepository]:
    repository = FakeEvidenceLifecycleRepository()
    return (
        EvidenceLifecycleService(
            repository=repository,
            storage=FakeObjectStorage(),
            job_producer=FakeJobProducer(),
            max_evidence_bytes=1024 * 1024,
            worker_lease_seconds=60,
        ),
        repository,
    )


def _context() -> UploadContext:
    return UploadContext(now=datetime.now(UTC), request_id="phase4-test")


async def _claimed_media_job(
    service: EvidenceLifecycleService, repository: FakeEvidenceLifecycleRepository
):
    job = make_job_record(
        source_type=SourceType.IMAGE,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        input_object_uri="cases/media/evidence/original",
    )
    repository.jobs[job.job_id] = job
    claim = await service.claim_job(
        processor_name="media_detection_v1", processor_version="1.0.0", context=_context()
    )
    assert claim.job is not None and claim.claim_token is not None
    return claim.job, claim.claim_token


def _manifest(job, *, configuration_hash: str = "config-a"):
    return build_manifest(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        job_id=job.job_id,
        source_type=job.source_type.value,
        processor_name=job.processor_name,
        processor_version=job.processor_version,
        input_object_uri=job.input_object_uri,
        configuration_hash=configuration_hash,
        chunks=(
            ChunkSpec(index=0, boundary=ChunkBoundary(time_start_ms=0, time_end_ms=1000)),
            ChunkSpec(index=1, boundary=ChunkBoundary(time_start_ms=1000, time_end_ms=2000)),
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_partial_chunk_persists_observations_artifact_and_checkpoint_atomically() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_media_job(service, repository)
    manifest = _manifest(job)
    assert (await service.create_media_manifest(manifest=manifest, context=_context())).created
    observation = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    batch = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="media-0",
        idempotency_key="media-0",
        observations=[observation],
    )
    publication = MediaChunkPublication(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        chunk_id=chunk_identity(manifest, 0),
        chunk_index=0,
        batch=batch,
        artifacts=(
            ArtifactRegistration(
                artifact_id=uuid4(),
                idempotency_key="thumbnail-0",
                parent_evidence_id=job.evidence_id,
                object_uri="s3://tracex-derived/thumbnail-0.jpg",
                artifact_kind="thumbnail",
                content_type="image/jpeg",
                sha256="a" * 64,
                byte_size=10,
                producer_version="1.0.0",
                configuration_hash="config-a",
            ),
        ),
        checkpoint_id=uuid4(),
        completed_at=datetime.now(UTC),
    )

    first = await service.submit_observation_batch(
        job_id=job.job_id,
        claim_token=claim_token,
        submission=batch,
        media_publication=publication,
        context=_context(),
    )
    second = await service.submit_observation_batch(
        job_id=job.job_id,
        claim_token=claim_token,
        submission=batch,
        media_publication=publication,
        context=_context(),
    )
    assert first.status is BatchAcceptanceStatus.ACCEPTED
    assert second.status is BatchAcceptanceStatus.REPLAYED
    assert repository.observations[observation.observation_id].canonical_payload["source_locator"]
    assert len(repository.graph_projection_jobs) == len(repository.observations) == 1
    assert len(repository.media_artifacts) == len(repository.media_checkpoints) == 1
    with pytest.raises(MediaPublicationConflictError):
        await service.submit_observation_batch(
            job_id=job.job_id,
            claim_token=claim_token,
            submission=batch,
            media_publication=publication.model_copy(update={"checkpoint_id": uuid4()}),
            context=_context(),
        )
    checkpoint = await service.get_media_resume_checkpoint(
        case_id=job.case_id, job_id=job.job_id, manifest_id=manifest.manifest_id
    )
    assert checkpoint is not None
    assert checkpoint.completed_chunk_ids == [publication.chunk_id]
    with pytest.raises(MediaManifestValidationError):
        await service.get_media_resume_checkpoint(
            case_id=uuid4(), job_id=job.job_id, manifest_id=manifest.manifest_id
        )


async def test_cross_case_artifact_and_changed_chunk_replay_are_rejected() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_media_job(service, repository)
    manifest = _manifest(job)
    await service.create_media_manifest(manifest=manifest, context=_context())
    batch = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="media-safe",
        idempotency_key="media-safe",
    )
    unsafe = MediaChunkPublication(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        chunk_id=chunk_identity(manifest, 0),
        chunk_index=0,
        batch=batch,
        artifacts=(
            ArtifactRegistration(
                artifact_id=uuid4(),
                idempotency_key="bad",
                parent_evidence_id=uuid4(),
                object_uri="s3://tracex-derived/a",
                artifact_kind="crop",
                content_type="image/jpeg",
                sha256="b" * 64,
                producer_version="1",
                configuration_hash="config-a",
            ),
        ),
        checkpoint_id=uuid4(),
        completed_at=datetime.now(UTC),
    )
    with pytest.raises(MediaManifestValidationError):
        await service.submit_observation_batch(
            job_id=job.job_id,
            claim_token=claim_token,
            submission=batch,
            media_publication=unsafe,
            context=_context(),
        )


def test_artifact_uri_cannot_contain_inline_bytes_or_credentials() -> None:
    with pytest.raises(ValidationError):
        ArtifactRegistration(
            artifact_id=uuid4(),
            idempotency_key="unsafe-uri",
            parent_evidence_id=uuid4(),
            object_uri="s3://user:secret@example/object",
            artifact_kind="crop",
            content_type="image/jpeg",
            sha256="c" * 64,
            producer_version="1",
            configuration_hash="config",
        )
