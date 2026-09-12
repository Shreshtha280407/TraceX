"""Scenarios 1-5: job claiming, concurrency, processor matching, lease expiry.

No PostgreSQL: `FakeEvidenceLifecycleRepository` mirrors the real
repository's claim semantics (including the `worker_results.job_id`
uniqueness and expired-lease-reclaim rules) closely enough to exercise
`service.py`'s orchestration. `tests/integration/evidence_lifecycle/` proves
the same behavior against a real `FOR UPDATE SKIP LOCKED` row lock.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.contracts.evidence import EvidenceClassification, SourceType
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository


def _service(
    repository: FakeEvidenceLifecycleRepository | None = None,
) -> tuple[EvidenceLifecycleService, FakeEvidenceLifecycleRepository]:
    repository = repository or FakeEvidenceLifecycleRepository()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=60,
    )
    return service, repository


def _context(now: datetime | None = None) -> UploadContext:
    return UploadContext(now=now or datetime.now(UTC), request_id="req-test")


async def _upload_document(service: EvidenceLifecycleService) -> None:
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


# --- Scenario 1: one eligible queued job can be claimed ---------------------


async def test_eligible_queued_job_can_be_claimed() -> None:
    service, repository = _service()
    await _upload_document(service)

    outcome = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context()
    )

    assert outcome.job is not None
    assert outcome.claim_token is not None
    assert outcome.lease_expires_at is not None
    assert outcome.job.attempt == 1
    assert repository.jobs[outcome.job.job_id].status.value == "running"
    assert repository.jobs[outcome.job.job_id].claimed_by == "fir_report_text_v1"


# --- Scenario 2: two concurrent claims cannot claim the same job ------------


async def test_two_concurrent_claims_only_one_succeeds() -> None:
    service, _ = _service()
    await _upload_document(service)

    first, second = await asyncio.gather(
        service.claim_job(
            processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context()
        ),
        service.claim_job(
            processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context()
        ),
    )

    winners = [outcome for outcome in (first, second) if outcome.job is not None]
    losers = [outcome for outcome in (first, second) if outcome.job is None]
    assert len(winners) == 1
    assert len(losers) == 1


# --- Scenario 3: no-work claim returns safely --------------------------------


async def test_claim_with_no_eligible_job_returns_safely() -> None:
    service, _ = _service()
    outcome = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context()
    )
    assert outcome.job is None
    assert outcome.claim_token is None
    assert outcome.lease_expires_at is None


# --- Scenario 4: processor mismatch cannot claim a job ----------------------


async def test_processor_mismatch_cannot_claim_job() -> None:
    service, _ = _service()
    await _upload_document(service)

    outcome = await service.claim_job(
        processor_name="cdr_generic_v1", processor_version="1.0.0", context=_context()
    )
    assert outcome.job is None

    wrong_version = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="9.9.9", context=_context()
    )
    assert wrong_version.job is None


# --- Scenario 5: expired lease is safely recoverable ------------------------


async def test_expired_lease_is_reclaimed_with_incremented_attempt() -> None:
    service, repository = _service()
    await _upload_document(service)

    first_claim_time = datetime.now(UTC)
    first = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(first_claim_time),
    )
    assert first.job is not None
    assert first.job.attempt == 1

    # Not yet expired: a second claim attempt finds nothing eligible.
    still_running = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(first_claim_time + timedelta(seconds=1)),
    )
    assert still_running.job is None

    # After the 60s lease has expired, the job becomes eligible again.
    reclaim_time = first_claim_time + timedelta(seconds=120)
    second = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(reclaim_time),
    )
    assert second.job is not None
    assert second.job.job_id == first.job.job_id
    assert second.job.attempt == 2  # incremented -- this is a reclaim, not a first claim
    assert second.claim_token != first.claim_token

    # The original (now-superseded) claim token no longer matches the job row.
    stored = repository.jobs[first.job.job_id]
    assert first.claim_token is not None
    assert stored.claim_token_hash != hashlib.sha256(first.claim_token.encode()).hexdigest()


# --- Retry exhaustion (Phase 3, Aditya) --------------------------------------


async def test_job_at_max_attempts_cannot_be_reclaimed_and_is_marked_failed() -> None:
    """A job whose lease keeps expiring is reclaimed up to `max_attempts` times, then
    transitions durably to `failed` (`retry_exhausted`) instead of being reclaimed again."""
    repository = FakeEvidenceLifecycleRepository()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=60,
        worker_job_max_attempts=2,
    )
    await _upload_document(service)

    t0 = datetime.now(UTC)
    first = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context(t0)
    )
    assert first.job is not None
    assert first.job.attempt == 1

    # Lease expires -> reclaimed once, reaching attempt 2 == max_attempts.
    t1 = t0 + timedelta(seconds=120)
    second = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context(t1)
    )
    assert second.job is not None
    assert second.job.attempt == 2

    # Lease expires again, but attempt 2 already == max_attempts: no further
    # reclaim is possible. This same claim call sweeps the job to `failed`.
    t2 = t1 + timedelta(seconds=120)
    third = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context(t2)
    )
    assert third.job is None
    assert len(third.retry_exhausted) == 1
    exhausted_job = third.retry_exhausted[0]
    assert exhausted_job.job_id == first.job.job_id
    assert exhausted_job.attempt == 2

    stored = repository.jobs[first.job.job_id]
    assert stored.status.value == "failed"
    assert stored.last_error_code == "retry_exhausted"

    result = repository.results[next(iter(repository.results))]
    assert result.job_id == first.job.job_id
    assert result.error_code == "retry_exhausted"
    assert result.status.value == "failed"

    # Permanently unclaimable from here on -- exhausted, not merely expired.
    fourth = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(t2 + timedelta(seconds=1)),
    )
    assert fourth.job is None
    assert fourth.retry_exhausted == ()


async def test_retry_exhaustion_sweep_is_processor_agnostic() -> None:
    """The sweep runs on every `claim_job` call, regardless of which processor is being
    claimed -- an exhausted job for a *different* processor is still swept."""
    repository = FakeEvidenceLifecycleRepository()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=60,
        worker_job_max_attempts=1,
    )
    await _upload_document(service)  # routes to fir_report_text_v1

    t0 = datetime.now(UTC)
    claimed = await service.claim_job(
        processor_name="fir_report_text_v1", processor_version="1.0.0", context=_context(t0)
    )
    assert claimed.job is not None
    assert claimed.job.attempt == 1  # already == max_attempts=1

    # A completely unrelated processor's claim call still sweeps it.
    t1 = t0 + timedelta(seconds=120)
    other = await service.claim_job(
        processor_name="cdr_generic_v1", processor_version="1.0.0", context=_context(t1)
    )
    assert other.job is None
    assert len(other.retry_exhausted) == 1
    assert other.retry_exhausted[0].job_id == claimed.job.job_id
    assert repository.jobs[claimed.job.job_id].status.value == "failed"
