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
