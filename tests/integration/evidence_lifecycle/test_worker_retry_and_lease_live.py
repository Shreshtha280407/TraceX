"""Aditya Phase 3: lease renewal, absolute lease lifetime, retry exhaustion, and
concurrent-terminal-submission safety against real PostgreSQL.

Mirrors `test_worker_lifecycle_live.py`'s pattern exactly: drives
`EvidenceLifecycleService` directly (not through HTTP) against the real
`repository`/`minio_storage` fixtures from `conftest.py`, with fully
controllable `UploadContext.now` values -- no need to actually sleep for a
lease to expire, or to poke the database with raw SQL to simulate the
passage of time. Self-skips under the exact same conditions as every other
file in this package (no `.env`, PostgreSQL/MinIO unreachable).
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.worker import WorkerStatus
from app.modules.evidence_lifecycle.errors import InvalidClaimTokenError, ResultConflictError
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.repository import EvidenceLifecycleRepository
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import MinioObjectStorage
from tests.fixtures.factories import make_observation, make_worker_result


def _upload_file(content: bytes, *, content_type: str = "text/csv", filename: str = "fixture"):
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


async def _upload_and_claim(
    service: EvidenceLifecycleService,
    *,
    case_id: UUID,
    user_id: UUID,
    cleanup_evidence_ids: list[UUID],
    now: datetime,
    content: bytes = b"caller_number,timestamp\n9990001234,2026-01-01T10:00:00Z\n",
):
    upload_outcome = await service.upload_evidence(
        case_id=case_id,
        uploaded_by=user_id,
        source_type=SourceType.CDR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=_upload_file(content),
        idempotency_key=None,
        context=UploadContext(now=now, request_id="live-retry-lease-test"),
    )
    cleanup_evidence_ids.append(upload_outcome.evidence.evidence_id)
    claim = await service.claim_job(
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        context=UploadContext(now=now, request_id="live-retry-lease-test"),
    )
    assert claim.job is not None
    assert claim.claim_token is not None
    return upload_outcome, claim


async def test_real_lease_renewal_extends_expiry_and_respects_the_absolute_ceiling(
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
        worker_lease_max_seconds=90,
    )
    t0 = datetime.now(UTC)
    _upload_outcome, claim = await _upload_and_claim(
        service,
        case_id=seeded_case,
        user_id=seeded_user,
        cleanup_evidence_ids=cleanup_evidence_ids,
        now=t0,
    )
    assert claim.job is not None and claim.claim_token is not None
    # Initial claim's lease: t0 + 60s (well under the 90s ceiling).

    # Requested at t0+40s (still within the current t0+60s lease, so this
    # renewal itself is valid): a full 60s extension would land at t0+100s,
    # past the 90s absolute ceiling from `claimed_at` -- capped to t0+90s,
    # never granted in full.
    capped = await service.renew_claim(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        context=UploadContext(now=t0 + timedelta(seconds=40), request_id="live-retry-lease-test"),
    )
    assert capped.lease_expires_at == t0 + timedelta(seconds=90)

    # Once real time passes the ceiling, renewal is rejected as expired --
    # never silently revived past the configured absolute maximum.
    try:
        await service.renew_claim(
            job_id=claim.job.job_id,
            claim_token=claim.claim_token,
            context=UploadContext(
                now=t0 + timedelta(seconds=91), request_id="live-retry-lease-test"
            ),
        )
        raise AssertionError("renewal past the absolute ceiling must be rejected")
    except InvalidClaimTokenError as exc:
        assert exc.reason == "lease_expired"


async def test_real_lease_expiry_then_reclaim_invalidates_the_old_claim_token(
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
    t0 = datetime.now(UTC)
    upload_outcome, first_claim = await _upload_and_claim(
        service,
        case_id=seeded_case,
        user_id=seeded_user,
        cleanup_evidence_ids=cleanup_evidence_ids,
        now=t0,
    )
    assert first_claim.job is not None and first_claim.claim_token is not None

    # Lease expires; a second claim attempt reclaims the exact same job,
    # incrementing its attempt and issuing a brand new claim token. Real
    # `FOR UPDATE SKIP LOCKED` concurrency-safe claiming is proven
    # elsewhere (`test_worker_claim.py`'s concurrent-claim scenario against
    # this same live database via `tests/integration/evidence_lifecycle/
    # test_evidence_lifecycle_live.py`'s sibling suite) -- this test's own
    # focus is the *old* token's fate after a real reclaim.
    t1 = t0 + timedelta(seconds=120)
    reclaim = await service.claim_job(
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        context=UploadContext(now=t1, request_id="live-retry-lease-test"),
    )
    assert reclaim.job is not None
    assert reclaim.job.job_id == first_claim.job.job_id
    assert reclaim.job.attempt == first_claim.job.attempt + 1
    assert reclaim.claim_token != first_claim.claim_token
    assert reclaim.was_reclaim is True

    result = make_worker_result(
        job_id=first_claim.job.job_id,
        case_id=seeded_case,
        evidence_id=upload_outcome.evidence.evidence_id,
        observations=[],
        error=None,
    )

    # The stale, pre-reclaim token can never submit a result for this job again.
    try:
        await service.submit_result(
            job_id=first_claim.job.job_id,
            claim_token=first_claim.claim_token,
            result=result,
            context=UploadContext(now=t1, request_id="live-retry-lease-test"),
        )
        raise AssertionError("the stale pre-reclaim claim token must be rejected")
    except InvalidClaimTokenError as exc:
        assert exc.reason == "token_mismatch"

    # The new claimant's token works normally.
    assert reclaim.claim_token is not None
    outcome = await service.submit_result(
        job_id=first_claim.job.job_id,
        claim_token=reclaim.claim_token,
        result=result,
        context=UploadContext(now=t1, request_id="live-retry-lease-test"),
    )
    assert outcome.status is WorkerStatus.SUCCEEDED


async def test_real_retry_exhaustion_transitions_the_job_to_failed(
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
        worker_job_max_attempts=2,
    )
    t0 = datetime.now(UTC)
    _upload_outcome, first_claim = await _upload_and_claim(
        service,
        case_id=seeded_case,
        user_id=seeded_user,
        cleanup_evidence_ids=cleanup_evidence_ids,
        now=t0,
    )
    assert first_claim.job is not None

    t1 = t0 + timedelta(seconds=120)
    second_claim = await service.claim_job(
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        context=UploadContext(now=t1, request_id="live-retry-lease-test"),
    )
    assert second_claim.job is not None
    assert second_claim.job.attempt == 2  # == worker_job_max_attempts

    # This job's lease expires again with no attempts remaining -- the next
    # claim call (for any processor) sweeps it to a durable `failed` state
    # rather than reclaiming it a third time.
    t2 = t1 + timedelta(seconds=120)
    third_claim = await service.claim_job(
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        context=UploadContext(now=t2, request_id="live-retry-lease-test"),
    )
    assert third_claim.job is None
    assert len(third_claim.retry_exhausted) == 1
    assert third_claim.retry_exhausted[0].job_id == first_claim.job.job_id

    persisted_job = await repository.get_job(seeded_case, first_claim.job.job_id)
    assert persisted_job is not None
    assert persisted_job.status is WorkerStatus.FAILED
    assert persisted_job.last_error_code == "retry_exhausted"

    persisted_result = await repository.get_result_for_job(first_claim.job.job_id)
    assert persisted_result is not None
    assert persisted_result.status is WorkerStatus.FAILED
    assert persisted_result.error_code == "retry_exhausted"


async def test_real_concurrent_terminal_result_submissions_produce_no_contradiction(
    repository: EvidenceLifecycleRepository,
    minio_storage: MinioObjectStorage,
    seeded_case: UUID,
    seeded_user: UUID,
    cleanup_evidence_ids: list[UUID],
) -> None:
    """Two genuinely concurrent `/result` submissions for the same claimed job -- one
    (and only one) wins; the loser gets a safe conflict, never a corrupted/duplicated
    persisted state."""
    await minio_storage.ensure_bucket()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=minio_storage,
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=60,
    )
    t0 = datetime.now(UTC)
    upload_outcome, claim = await _upload_and_claim(
        service,
        case_id=seeded_case,
        user_id=seeded_user,
        cleanup_evidence_ids=cleanup_evidence_ids,
        now=t0,
    )
    assert claim.job is not None and claim.claim_token is not None

    observation_a = make_observation(
        case_id=seeded_case, evidence_id=upload_outcome.evidence.evidence_id
    )
    observation_b = make_observation(
        case_id=seeded_case, evidence_id=upload_outcome.evidence.evidence_id
    )
    result_a = make_worker_result(
        job_id=claim.job.job_id,
        case_id=seeded_case,
        evidence_id=upload_outcome.evidence.evidence_id,
        observations=[observation_a],
        checkpoint="race-payload-a",
        error=None,
    )
    result_b = make_worker_result(
        job_id=claim.job.job_id,
        case_id=seeded_case,
        evidence_id=upload_outcome.evidence.evidence_id,
        observations=[observation_b],
        checkpoint="race-payload-b",
        error=None,
    )

    async def _submit(result):
        try:
            outcome = await service.submit_result(
                job_id=claim.job.job_id,
                claim_token=claim.claim_token,
                result=result,
                context=UploadContext(now=t0, request_id="live-retry-lease-test"),
            )
            return ("ok", outcome)
        except ResultConflictError as exc:
            return ("conflict", exc)

    outcome_a, outcome_b = await asyncio.gather(_submit(result_a), _submit(result_b))

    outcomes = [outcome_a, outcome_b]
    winners = [o for kind, o in outcomes if kind == "ok"]
    losers = [o for kind, o in outcomes if kind == "conflict"]
    assert len(winners) == 1, "exactly one concurrent submission must be durably accepted"
    assert len(losers) == 1, "the other must be safely rejected, never silently accepted too"

    persisted_job = await repository.get_job(seeded_case, claim.job.job_id)
    assert persisted_job is not None
    assert persisted_job.status is WorkerStatus.SUCCEEDED

    persisted_result = await repository.get_result_for_job(claim.job.job_id)
    assert persisted_result is not None
    persisted_observations = await repository.list_observations_for_result(
        persisted_result.result_id
    )
    # Exactly one winner's single observation -- never both, never neither.
    assert len(persisted_observations) == 1
