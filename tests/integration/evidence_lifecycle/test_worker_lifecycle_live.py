"""Scenario 17: real claim + result submission against live PostgreSQL/MinIO.

Self-skips (never fabricates a pass) if there's no `.env` at the repo root,
or if PostgreSQL/MinIO specifically aren't reachable through it -- mirrors
`tests/integration/evidence_lifecycle/test_evidence_lifecycle_live.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.worker import WorkerStatus
from app.modules.evidence_lifecycle.errors import InvalidClaimTokenError
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import MinioObjectStorage
from tests.fixtures.factories import make_observation, make_worker_result


def _upload_file(content: bytes, *, content_type: str = "text/csv", filename: str = "fixture"):
    import io

    from fastapi import UploadFile
    from starlette.datastructures import Headers

    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


async def test_real_claim_and_result_submission_against_live_infra(
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
        worker_lease_seconds=60,
    )

    upload_outcome = await service.upload_evidence(
        case_id=seeded_case,
        uploaded_by=seeded_user,
        source_type=SourceType.CDR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=_upload_file(b"caller_number,timestamp\n9990001234,2026-01-01T10:00:00Z\n"),
        idempotency_key=None,
        context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test"),
    )
    cleanup_evidence_ids.append(upload_outcome.evidence.evidence_id)

    claim = await service.claim_job(
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test"),
    )
    assert claim.job is not None
    assert claim.job.job_id == upload_outcome.job.job_id
    assert claim.claim_token is not None

    observation = make_observation(
        case_id=seeded_case, evidence_id=upload_outcome.evidence.evidence_id
    )
    result = make_worker_result(
        job_id=claim.job.job_id,
        case_id=seeded_case,
        evidence_id=upload_outcome.evidence.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[observation],
        error=None,
    )

    outcome = await service.submit_result(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        result=result,
        context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test"),
    )
    assert outcome.status is WorkerStatus.SUCCEEDED
    assert outcome.observation_ids == (observation.observation_id,)

    persisted_job = await repository.get_job(seeded_case, claim.job.job_id)
    assert persisted_job is not None
    assert persisted_job.status is WorkerStatus.SUCCEEDED  # durable terminal state

    persisted_result = await repository.get_result_for_job(claim.job.job_id)
    assert persisted_result is not None
    assert persisted_result.status is WorkerStatus.SUCCEEDED

    persisted_observations = await repository.list_observations_for_result(
        persisted_result.result_id
    )
    assert len(persisted_observations) == 1
    assert persisted_observations[0].observation_id == observation.observation_id

    # A stale/replayed claim token is safely rejected once the job is terminal
    # and the payload actually differs -- proves the live conflict path too.
    conflicting_result = make_worker_result(
        job_id=claim.job.job_id,
        case_id=seeded_case,
        evidence_id=upload_outcome.evidence.evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        error={"code": "different", "message": "different outcome", "retryable": False},
    )
    with pytest.raises(Exception):  # noqa: B017 - ResultConflictError, re-raised unmodified
        await service.submit_result(
            job_id=claim.job.job_id,
            claim_token=claim.claim_token,
            result=conflicting_result,
            context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test"),
        )


async def test_real_claim_with_wrong_processor_finds_nothing(
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
        worker_lease_seconds=60,
    )
    upload_outcome = await service.upload_evidence(
        case_id=seeded_case,
        uploaded_by=seeded_user,
        source_type=SourceType.DOCUMENT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=_upload_file(
            b"FIR No. 1/2026", content_type="text/plain", filename="fir_fixture.txt"
        ),
        idempotency_key=None,
        context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test-2"),
    )
    cleanup_evidence_ids.append(upload_outcome.evidence.evidence_id)

    claim = await service.claim_job(
        processor_name="cdr_generic_v1",  # wrong processor for a DOCUMENT job
        processor_version="1.0.0",
        context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test-2"),
    )
    assert claim.job is None

    with pytest.raises(InvalidClaimTokenError):
        await service.submit_result(
            job_id=upload_outcome.job.job_id,
            claim_token="never-claimed",
            result=make_worker_result(
                job_id=upload_outcome.job.job_id,
                case_id=seeded_case,
                evidence_id=upload_outcome.evidence.evidence_id,
                error=None,
            ),
            context=UploadContext(now=datetime.now(UTC), request_id="live-worker-test-2"),
        )
