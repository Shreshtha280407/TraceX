"""Scenarios 1-12, 15: `EvidenceLifecycleService.upload_evidence` unit tests.

No PostgreSQL, MinIO, or Redis: `FakeEvidenceLifecycleRepository`,
`FakeObjectStorage`, and `FakeJobProducer` exercise every branch of the
upload -> hash -> store -> persist -> publish flow in-process.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy.exc

from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.worker import WorkerJobV1
from app.modules.evidence_lifecycle.errors import (
    EmptyUploadError,
    IdempotencyConflictError,
    MissingFilenameError,
    PayloadTooLargeError,
    UnsupportedContentTypeError,
    UnsupportedSourceTypeError,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository

DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def _service(
    *,
    repository: FakeEvidenceLifecycleRepository | None = None,
    storage: FakeObjectStorage | None = None,
    job_producer: FakeJobProducer | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[
    EvidenceLifecycleService, FakeEvidenceLifecycleRepository, FakeObjectStorage, FakeJobProducer
]:
    repository = repository or FakeEvidenceLifecycleRepository()
    storage = storage or FakeObjectStorage()
    job_producer = job_producer or FakeJobProducer()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=storage,
        job_producer=job_producer,
        max_evidence_bytes=max_bytes,
    )
    return service, repository, storage, job_producer


def _context() -> UploadContext:
    return UploadContext(now=datetime.now(UTC), request_id="req-test")


# --- Scenario 1: valid upload creates evidence + storage write + job -------


async def test_valid_upload_creates_evidence_storage_write_and_job() -> None:
    service, repository, storage, job_producer = _service()
    case_id = uuid4()
    uploaded_by = uuid4()
    content = b"FIR No. 123/2026 filed at City Police Station."

    outcome = await service.upload_evidence(
        case_id=case_id,
        uploaded_by=uploaded_by,
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=content, filename="fir.txt", content_type="text/plain"),
        idempotency_key=None,
        context=_context(),
    )

    assert outcome.created is True
    assert len(repository.evidence) == 1
    assert len(repository.jobs) == 1
    assert len(storage.objects) == 1
    assert storage.objects[outcome.evidence.object_uri] == content
    assert len(job_producer.published) == 1
    # A same-request successful publish is reflected immediately, not just on a later GET.
    assert outcome.job.dispatched_at is not None
    assert repository.jobs[outcome.job.job_id].dispatched_at is not None


# --- Scenario 2: correct SHA-256 -------------------------------------------


async def test_sha256_matches_uploaded_content() -> None:
    service, *_ = _service()
    content = b"exact bytes to hash"
    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=content),
        idempotency_key=None,
        context=_context(),
    )
    assert outcome.evidence.sha256 == hashlib.sha256(content).hexdigest()


# --- Scenario 3: bounded-chunk processing for large data -------------------


async def test_large_upload_is_hashed_correctly_via_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.evidence_lifecycle.service as service_module

    monkeypatch.setattr(service_module, "UPLOAD_CHUNK_BYTES", 16)
    content = b"x" * 1000 + b"y" * 1000  # several chunk-boundary crossings
    service, repository, storage, _ = _service(max_bytes=1_000_000)

    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=content),
        idempotency_key=None,
        context=_context(),
    )

    assert outcome.evidence.sha256 == hashlib.sha256(content).hexdigest()
    assert storage.objects[outcome.evidence.object_uri] == content


# --- Scenario 4: object keys contain no unsafe filename/path input ---------


async def test_object_key_never_contains_the_caller_supplied_filename() -> None:
    service, *_ = _service()
    case_id = uuid4()
    malicious_filename = "../../../etc/passwd; rm -rf /"
    outcome = await service.upload_evidence(
        case_id=case_id,
        uploaded_by=uuid4(),
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(filename=malicious_filename),
        idempotency_key=None,
        context=_context(),
    )
    assert malicious_filename not in outcome.evidence.object_uri
    assert outcome.evidence.object_uri == (
        f"cases/{case_id}/evidence/{outcome.evidence.evidence_id}/original"
    )
    # The filename is still preserved as display-only metadata, never used as a path.
    assert outcome.evidence.original_filename == malicious_filename


# --- Scenario 5: unsupported source/content type rejected ------------------


async def test_unsupported_content_type_for_source_type_is_rejected() -> None:
    service, repository, storage, job_producer = _service()
    with pytest.raises(UnsupportedContentTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content_type="video/mp4"),
            idempotency_key=None,
            context=_context(),
        )
    assert not repository.evidence
    assert not storage.objects
    assert not job_producer.published


async def test_source_type_other_has_no_registered_processor() -> None:
    service, *_ = _service()
    with pytest.raises(UnsupportedSourceTypeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.OTHER,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content_type="application/octet-stream"),
            idempotency_key=None,
            context=_context(),
        )


# --- Scenario 6: empty upload rejected --------------------------------------


async def test_empty_upload_is_rejected() -> None:
    service, repository, storage, _ = _service()
    with pytest.raises(EmptyUploadError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content=b""),
            idempotency_key=None,
            context=_context(),
        )
    assert not repository.evidence
    assert not storage.objects


async def test_missing_filename_is_rejected() -> None:
    service, *_ = _service()
    with pytest.raises(MissingFilenameError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(filename=None),
            idempotency_key=None,
            context=_context(),
        )


# --- Scenario 7: oversized upload rejected ----------------------------------


async def test_oversized_upload_is_rejected() -> None:
    service, repository, storage, _ = _service(max_bytes=10)
    with pytest.raises(PayloadTooLargeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content=b"this payload is far larger than ten bytes"),
            idempotency_key=None,
            context=_context(),
        )
    assert not repository.evidence
    assert not storage.objects


# --- Scenario 8: idempotency-key retry returns original result -------------


async def test_idempotency_key_replay_returns_original_result_without_duplication() -> None:
    service, repository, storage, job_producer = _service()
    case_id = uuid4()
    content = b"identical content"
    kwargs = {
        "case_id": case_id,
        "uploaded_by": uuid4(),
        "source_type": SourceType.DOCUMENT,
        "classification": EvidenceClassification.UNCLASSIFIED,
        "parser_profile": None,
        "idempotency_key": "retry-key-1",
    }

    first = await service.upload_evidence(
        upload=make_upload_file(content=content), context=_context(), **kwargs
    )
    second = await service.upload_evidence(
        upload=make_upload_file(content=content), context=_context(), **kwargs
    )

    assert first.evidence.evidence_id == second.evidence.evidence_id
    assert first.job.job_id == second.job.job_id
    assert second.created is False
    assert len(repository.evidence) == 1
    assert len(repository.jobs) == 1
    assert len(storage.objects) == 1
    assert len(job_producer.published) == 1  # never published twice


# --- Scenario 9: conflicting idempotency-key reuse rejected -----------------


async def test_conflicting_idempotency_key_reuse_is_rejected() -> None:
    """The pre-check catches a sequential conflict before any storage write happens."""
    service, repository, storage, job_producer = _service()
    case_id = uuid4()
    uploaded_by = uuid4()

    await service.upload_evidence(
        case_id=case_id,
        uploaded_by=uploaded_by,
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=b"original content"),
        idempotency_key="shared-key",
        context=_context(),
    )

    with pytest.raises(IdempotencyConflictError):
        await service.upload_evidence(
            case_id=case_id,
            uploaded_by=uploaded_by,
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content=b"a completely different payload"),
            idempotency_key="shared-key",
            context=_context(),
        )

    assert len(repository.evidence) == 1  # only the first upload persisted
    assert len(storage.objects) == 1
    assert not storage.deleted_keys  # rejected before any second object write
    assert len(job_producer.published) == 1


async def test_concurrent_idempotency_key_race_cleans_up_the_losing_object() -> None:
    """The loser of a true race reaches storage.put_object before the DB conflict surfaces."""

    class _RacyRepository(FakeEvidenceLifecycleRepository):
        async def create_evidence_with_job(self, evidence, job) -> None:  # type: ignore[override]
            # Simulate a concurrent winner having already committed a
            # conflicting row (different content) between this call's
            # pre-check and its own insert attempt.
            self.evidence[uuid4()] = evidence.model_copy(
                update={"evidence_id": uuid4(), "sha256": "0" * 64}
            )
            raise sqlalchemy.exc.IntegrityError(
                "duplicate upload_idempotency_key", {}, Exception("unique violation")
            )

    repository = _RacyRepository()
    storage = FakeObjectStorage()
    service, _, _, _ = _service(repository=repository, storage=storage)

    with pytest.raises(IdempotencyConflictError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(content=b"racing payload"),
            idempotency_key="race-key",
            context=_context(),
        )

    # The loser's own object write was cleaned up once the conflict surfaced.
    assert not storage.objects
    assert len(storage.deleted_keys) == 1


# --- Scenario 10: storage failure doesn't publish a job ---------------------


async def test_storage_failure_leaves_no_evidence_job_or_publish() -> None:
    storage = FakeObjectStorage(fail_put=True)
    service, repository, _, job_producer = _service(storage=storage)

    with pytest.raises(Exception):  # noqa: B017 - StorageError, re-raised unmodified
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(),
            idempotency_key=None,
            context=_context(),
        )

    assert not repository.evidence
    assert not repository.jobs
    assert not job_producer.published


# --- Scenario 11: DB failure recovers with storage cleanup ------------------


async def test_db_persistence_failure_cleans_up_the_written_object() -> None:
    class _BrokenRepository(FakeEvidenceLifecycleRepository):
        async def create_evidence_with_job(self, evidence, job) -> None:  # type: ignore[override]
            raise RuntimeError("simulated connection drop")

    storage = FakeObjectStorage()
    service, _, _, job_producer = _service(repository=_BrokenRepository(), storage=storage)

    with pytest.raises(RuntimeError):
        await service.upload_evidence(
            case_id=uuid4(),
            uploaded_by=uuid4(),
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=make_upload_file(),
            idempotency_key=None,
            context=_context(),
        )

    assert not storage.objects  # the orphaned object was deleted
    assert len(storage.deleted_keys) == 1
    assert not job_producer.published


async def test_job_publication_failure_is_deferred_not_lost() -> None:
    job_producer = FakeJobProducer(fail=True)
    service, repository, _, _ = _service(job_producer=job_producer)

    outcome = await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(),
        idempotency_key=None,
        context=_context(),
    )

    # Upload itself succeeds -- the durable row exists even though the
    # best-effort Redis publish never landed.
    assert outcome.evidence.evidence_id in repository.evidence
    assert repository.jobs[outcome.job.job_id].dispatched_at is None


# --- Scenario 12: producer-generated WorkerJobV1 validates exactly ---------


async def test_generated_job_validates_against_the_frozen_worker_job_contract() -> None:
    job_producer = FakeJobProducer()
    service, *_ = _service(job_producer=job_producer)

    await service.upload_evidence(
        case_id=uuid4(),
        uploaded_by=uuid4(),
        source_type=SourceType.CDR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=b"a,b\n1,2\n", content_type="text/csv"),
        idempotency_key=None,
        context=_context(),
    )

    assert len(job_producer.published) == 1
    published = job_producer.published[0]
    assert isinstance(published, WorkerJobV1)
    # Round-trips through the contract's own validators without raising.
    WorkerJobV1.model_validate(published.model_dump(mode="json"))
    assert published.processor_name == "cdr_generic_v1"
    assert published.attempt == 1
