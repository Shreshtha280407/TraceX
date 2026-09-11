"""Scenario 17-18 (paraphrased): migration integrity against a live database,
and one real end-to-end flow against live PostgreSQL + MinIO.

Self-skips (never fabricates a pass) if there's no `.env` at the repo root,
or if PostgreSQL/MinIO specifically aren't reachable through it -- mirrors
every other `tests/integration/*` suite in this repository.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy.exc

from app.contracts.evidence import EvidenceClassification, SourceType
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import MinioObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_evidence_record, make_job_record


async def test_migration_created_tables_enforce_uniqueness_and_fks(
    repository: EvidenceLifecycleRepository,
    seeded_case: UUID,
    seeded_user: UUID,
    cleanup_evidence_ids: list[UUID],
) -> None:
    now = datetime.now(UTC)
    evidence = make_evidence_record(
        case_id=seeded_case,
        uploaded_by=seeded_user,
        uploaded_at=now,
        created_at=now,
        updated_at=now,
        upload_idempotency_key="live-key-1",
    )
    job = make_job_record(
        case_id=seeded_case,
        evidence_id=evidence.evidence_id,
        requested_at=now,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    cleanup_evidence_ids.append(evidence.evidence_id)

    await repository.create_evidence_with_job(evidence, job)

    fetched = await repository.get_evidence(seeded_case, evidence.evidence_id)
    assert fetched is not None
    assert fetched.sha256 == evidence.sha256

    fetched_job = await repository.get_job(seeded_case, job.job_id)
    assert fetched_job is not None
    assert fetched_job.status.value == "queued"

    # Same case + same Idempotency-Key a second time violates the partial unique index.
    duplicate_evidence = make_evidence_record(
        case_id=seeded_case,
        evidence_id=uuid4(),
        uploaded_by=seeded_user,
        uploaded_at=now,
        created_at=now,
        updated_at=now,
        upload_idempotency_key="live-key-1",
    )
    duplicate_job = make_job_record(
        case_id=seeded_case,
        evidence_id=duplicate_evidence.evidence_id,
        requested_at=now,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await repository.create_evidence_with_job(duplicate_evidence, duplicate_job)


async def test_real_upload_flow_against_live_postgres_and_minio(
    repository: EvidenceLifecycleRepository,
    minio_storage: MinioObjectStorage,
    seeded_case: UUID,
    seeded_user: UUID,
    cleanup_evidence_ids: list[UUID],
) -> None:
    await minio_storage.ensure_bucket()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=minio_storage,
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
    )
    content = b"Synthetic FIR text fixture for live integration testing only."

    outcome = await service.upload_evidence(
        case_id=seeded_case,
        uploaded_by=seeded_user,
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=_upload_file(content),
        idempotency_key=None,
        context=UploadContext(now=datetime.now(UTC), request_id="live-test"),
    )
    cleanup_evidence_ids.append(outcome.evidence.evidence_id)

    assert outcome.evidence.sha256 == hashlib.sha256(content).hexdigest()
    assert minio_storage.read_bytes(outcome.evidence.object_uri) == content

    persisted = await repository.get_evidence(seeded_case, outcome.evidence.evidence_id)
    assert persisted is not None
    assert persisted.processing_status.value == "queued"

    persisted_job = await repository.get_job(seeded_case, outcome.job.job_id)
    assert persisted_job is not None
    assert persisted_job.status.value == "queued"  # durable, queued -- no consumer in this phase

    await minio_storage.delete_object(outcome.evidence.object_uri)


def _upload_file(content: bytes):
    import io

    from fastapi import UploadFile
    from starlette.datastructures import Headers

    return UploadFile(
        file=io.BytesIO(content),
        filename="fir_fixture.txt",
        headers=Headers({"content-type": "text/plain"}),
    )
