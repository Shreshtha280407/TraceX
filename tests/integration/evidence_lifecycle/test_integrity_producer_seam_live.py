"""Phase 6 producer-seam coverage: the `evidence_registered` integrity hook.

Proof point 17: existing evidence-upload behavior stays fully backward
compatible, both when no integrity recorder is wired in at all (every
pre-existing call site and test) and when a real one is present but its
underlying store is unreachable (the safe, non-blocking wrapper contract).
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from uuid import UUID

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.contracts.evidence import EvidenceClassification, SourceType
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import MinioObjectStorage
from app.modules.integrity.models import IntegrityEventKind
from app.modules.integrity.repository import IntegrityRepository
from app.modules.integrity.repository import create_engine as create_integrity_engine
from app.modules.integrity.service import IntegrityService
from app.modules.integrity.signing import generate_signing_key_b64
from tests.integration.evidence_lifecycle.conftest import _live_settings


def _upload_file(content: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename="fir_fixture.txt",
        headers=Headers({"content-type": "text/plain"}),
    )


async def test_upload_still_succeeds_with_no_integrity_recorder(
    repository: EvidenceLifecycleRepository,
    minio_storage: MinioObjectStorage,
    seeded_case: UUID,
    seeded_user: UUID,
    cleanup_evidence_ids: list[UUID],
) -> None:
    """Every pre-existing call site omits `integrity_recorder` -- must behave identically."""
    await minio_storage.ensure_bucket()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=minio_storage,
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
    )
    content = b"Synthetic fixture, no integrity recorder wired in."

    outcome = await service.upload_evidence(
        case_id=seeded_case,
        uploaded_by=seeded_user,
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=_upload_file(content),
        idempotency_key=None,
        context=UploadContext(now=datetime.now(UTC), request_id="integrity-seam-off"),
    )
    cleanup_evidence_ids.append(outcome.evidence.evidence_id)

    assert outcome.evidence.sha256 == hashlib.sha256(content).hexdigest()
    await minio_storage.delete_object(outcome.evidence.object_uri)


async def test_upload_records_exactly_one_evidence_registered_event(
    repository: EvidenceLifecycleRepository,
    minio_storage: MinioObjectStorage,
    seeded_case: UUID,
    seeded_user: UUID,
    cleanup_evidence_ids: list[UUID],
) -> None:
    await minio_storage.ensure_bucket()
    settings = _live_settings(integrity_signing_key=generate_signing_key_b64())
    integrity_engine = create_integrity_engine(settings)
    integrity_repository = IntegrityRepository(integrity_engine)
    integrity_recorder = IntegrityService(integrity_repository, settings)
    service = EvidenceLifecycleService(
        repository=repository,
        storage=minio_storage,
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        integrity_recorder=integrity_recorder,
    )
    content = b"Synthetic fixture, real integrity recorder wired in."

    try:
        outcome = await service.upload_evidence(
            case_id=seeded_case,
            uploaded_by=seeded_user,
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=_upload_file(content),
            idempotency_key=None,
            context=UploadContext(now=datetime.now(UTC), request_id="integrity-seam-on"),
        )
        cleanup_evidence_ids.append(outcome.evidence.evidence_id)
        await minio_storage.delete_object(outcome.evidence.object_uri)

        events = await integrity_repository.list_events_in_range(seeded_case, 1, 10_000)
        matching = [
            e
            for e in events
            if e.event_kind == IntegrityEventKind.EVIDENCE_REGISTERED
            and e.subject_id == str(outcome.evidence.evidence_id)
        ]
        assert len(matching) == 1
        assert matching[0].canonical_payload_sha256  # a hash, never raw content
    finally:
        # `integrity_events` is genuinely append-only (the PostgreSQL trigger
        # `c42d3e4f5a6b`'s migration adds -- see
        # `docs/architecture/phase-6-integrity.md`'s "Deliberate storage
        # trade-off" section): a direct `DELETE` here is correctly rejected,
        # confirmed live during the Phase 6 Part 5 gate. This mirrors
        # `tests/integration/integrity/conftest.py`'s own precedent -- rely
        # on `seeded_case` being a fresh, unguessable ID per test run rather
        # than deleting rows that cannot be deleted. Only the (non-append-
        # only) sequence counter is cleaned up.
        import sqlalchemy as sa

        from app.modules.integrity.repository import integrity_sequence_counters_table

        async with integrity_engine.begin() as conn:
            await conn.execute(
                sa.delete(integrity_sequence_counters_table).where(
                    integrity_sequence_counters_table.c.case_id == seeded_case
                )
            )
        await integrity_engine.dispose()


async def test_upload_still_succeeds_when_integrity_store_is_unreachable(
    repository: EvidenceLifecycleRepository,
    minio_storage: MinioObjectStorage,
    seeded_case: UUID,
    seeded_user: UUID,
    cleanup_evidence_ids: list[UUID],
) -> None:
    """The safe-recording contract: a broken integrity store must never fail the upload."""
    await minio_storage.ensure_bucket()
    broken_settings = _live_settings(
        integrity_signing_key=generate_signing_key_b64(),
        postgres_dsn="postgresql+asyncpg://tracex:wrong@localhost:1/does_not_exist",
    )
    broken_engine = create_integrity_engine(broken_settings)
    broken_recorder = IntegrityService(IntegrityRepository(broken_engine), broken_settings)
    service = EvidenceLifecycleService(
        repository=repository,
        storage=minio_storage,
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        integrity_recorder=broken_recorder,
    )
    content = b"Synthetic fixture, integrity store deliberately unreachable."

    try:
        outcome = await service.upload_evidence(
            case_id=seeded_case,
            uploaded_by=seeded_user,
            source_type=SourceType.DOCUMENT,
            classification=EvidenceClassification.UNCLASSIFIED,
            parser_profile=None,
            upload=_upload_file(content),
            idempotency_key=None,
            context=UploadContext(now=datetime.now(UTC), request_id="integrity-seam-broken"),
        )
        cleanup_evidence_ids.append(outcome.evidence.evidence_id)
        assert outcome.evidence.sha256 == hashlib.sha256(content).hexdigest()
        await minio_storage.delete_object(outcome.evidence.object_uri)
    finally:
        await broken_engine.dispose()
