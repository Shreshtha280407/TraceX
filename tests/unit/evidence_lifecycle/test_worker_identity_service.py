"""Scenarios 8, 9, 10, 11, 12, 13: worker-identity binding at the service layer.

Mirrors `test_worker_claim.py`/`test_worker_result.py`'s pattern exactly
(`FakeEvidenceLifecycleRepository`, no HTTP) -- these tests exercise
`EvidenceLifecycleService.claim_job`/`submit_result`/
`get_claimed_evidence_input`'s new `claimed_by_worker_id`/`worker_id`
parameters directly, independent of `require_worker_principal`'s HTTP-layer
authentication (covered separately in `test_worker_identity_api.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.contracts.evidence import EvidenceClassification, SourceType
from app.modules.evidence_lifecycle.errors import InvalidClaimTokenError
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import make_observation, make_worker_result


def _service(
    *, worker_lease_seconds: int = 60
) -> tuple[EvidenceLifecycleService, FakeEvidenceLifecycleRepository]:
    repository = FakeEvidenceLifecycleRepository()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=worker_lease_seconds,
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


# --- Scenario 8: claiming binds claimed_by_worker_id -------------------------


async def test_claim_binds_the_authenticated_worker_identity() -> None:
    service, repository = _service()
    await _upload_document(service)
    worker_id = uuid4()

    outcome = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(),
        claimed_by_worker_id=worker_id,
    )

    assert outcome.job is not None
    assert repository.jobs[outcome.job.job_id].claimed_by_worker_id == worker_id


# --- Scenarios 9-10: a second worker cannot use another worker's claim -------


async def test_second_worker_cannot_fetch_input_with_first_workers_claim_token() -> None:
    service, _repository = _service()
    await _upload_document(service)
    worker_a, worker_b = uuid4(), uuid4()

    claim = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(),
        claimed_by_worker_id=worker_a,
    )
    assert claim.job is not None and claim.claim_token is not None

    with pytest.raises(InvalidClaimTokenError) as excinfo:
        await service.get_claimed_evidence_input(
            job_id=claim.job.job_id,
            claim_token=claim.claim_token,
            context=_context(),
            worker_id=worker_b,
        )
    assert excinfo.value.reason == "worker_identity_mismatch"

    # The rightful claimant is unaffected by worker_b's attempt.
    resolved = await service.get_claimed_evidence_input(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        context=_context(),
        worker_id=worker_a,
    )
    assert resolved.evidence_id == claim.job.evidence_id


async def test_second_worker_cannot_submit_another_workers_result() -> None:
    service, repository = _service()
    await _upload_document(service)
    worker_a, worker_b = uuid4(), uuid4()

    claim = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(),
        claimed_by_worker_id=worker_a,
    )
    assert claim.job is not None and claim.claim_token is not None
    result = make_worker_result(
        job_id=claim.job.job_id,
        case_id=claim.job.case_id,
        evidence_id=claim.job.evidence_id,
        observations=[],
        error=None,
    )

    with pytest.raises(InvalidClaimTokenError) as excinfo:
        await service.submit_result(
            job_id=claim.job.job_id,
            claim_token=claim.claim_token,
            result=result,
            context=_context(),
            worker_id=worker_b,
        )
    assert excinfo.value.reason == "worker_identity_mismatch"
    assert repository.jobs[claim.job.job_id].status.value == "running"  # never marked terminal

    # The rightful claimant's submission still succeeds afterward.
    outcome = await service.submit_result(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        result=result,
        context=_context(),
        worker_id=worker_a,
    )
    assert outcome.created is True


# --- Scenario 11: lease-expiry reclaim transfers ownership safely -----------


async def test_lease_expiry_reclaim_transfers_ownership_to_the_new_worker() -> None:
    service, repository = _service(worker_lease_seconds=60)
    await _upload_document(service)
    worker_a, worker_b = uuid4(), uuid4()

    first_claim_time = datetime.now(UTC)
    first = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(first_claim_time),
        claimed_by_worker_id=worker_a,
    )
    assert first.job is not None
    assert repository.jobs[first.job.job_id].claimed_by_worker_id == worker_a

    reclaim_time = first_claim_time + timedelta(seconds=120)  # past the 60s lease
    second = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(reclaim_time),
        claimed_by_worker_id=worker_b,
    )
    assert second.job is not None
    assert second.job.job_id == first.job.job_id
    assert repository.jobs[second.job.job_id].claimed_by_worker_id == worker_b

    # Worker A's superseded claim token and identity no longer work.
    assert first.claim_token is not None
    with pytest.raises(InvalidClaimTokenError):
        await service.get_claimed_evidence_input(
            job_id=first.job.job_id,
            claim_token=first.claim_token,
            context=_context(reclaim_time),
            worker_id=worker_a,
        )

    # Worker B, the new legitimate claimant, succeeds.
    assert second.claim_token is not None
    resolved = await service.get_claimed_evidence_input(
        job_id=second.job.job_id,
        claim_token=second.claim_token,
        context=_context(reclaim_time),
        worker_id=worker_b,
    )
    assert resolved.evidence_id == second.job.evidence_id


# --- Scenario 12: same-worker claim/result idempotency still works ----------


async def test_same_worker_result_resubmission_is_still_idempotent() -> None:
    service, repository = _service()
    await _upload_document(service)
    worker_id = uuid4()

    claim = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(),
        claimed_by_worker_id=worker_id,
    )
    assert claim.job is not None and claim.claim_token is not None
    result = make_worker_result(
        job_id=claim.job.job_id,
        case_id=claim.job.case_id,
        evidence_id=claim.job.evidence_id,
        observations=[
            make_observation(case_id=claim.job.case_id, evidence_id=claim.job.evidence_id)
        ],
        error=None,
    )

    first = await service.submit_result(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        result=result,
        context=_context(),
        worker_id=worker_id,
    )
    second = await service.submit_result(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        result=result,
        context=_context(),
        worker_id=worker_id,
    )

    assert first.created is True
    assert second.created is False
    assert second.result_id == first.result_id
    assert len(repository.results) == 1


# --- Scenario 13: cached/terminal paths still authenticate identity first ---


async def test_terminal_job_replay_still_checks_worker_identity_first() -> None:
    """The exact same token, presented by the wrong worker, is rejected even
    against an already-terminal (replayable) job -- identity verification is
    not skipped just because the fast idempotent-replay path would otherwise
    apply."""
    service, repository = _service()
    await _upload_document(service)
    worker_a, worker_b = uuid4(), uuid4()

    claim = await service.claim_job(
        processor_name="fir_report_text_v1",
        processor_version="1.0.0",
        context=_context(),
        claimed_by_worker_id=worker_a,
    )
    assert claim.job is not None and claim.claim_token is not None
    result = make_worker_result(
        job_id=claim.job.job_id,
        case_id=claim.job.case_id,
        evidence_id=claim.job.evidence_id,
        observations=[],
        error=None,
    )
    await service.submit_result(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        result=result,
        context=_context(),
        worker_id=worker_a,
    )
    assert repository.jobs[claim.job.job_id].status.value == "succeeded"  # now terminal

    # worker_b presents the *same, still hash-matching* claim token against
    # the now-terminal job -- must still be rejected on identity, not handed
    # the cached replay.
    with pytest.raises(InvalidClaimTokenError) as excinfo:
        await service.submit_result(
            job_id=claim.job.job_id,
            claim_token=claim.claim_token,
            result=result,
            context=_context(),
            worker_id=worker_b,
        )
    assert excinfo.value.reason == "worker_identity_mismatch"

    # worker_a, the rightful claimant, still gets the idempotent replay.
    replay = await service.submit_result(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        result=result,
        context=_context(),
        worker_id=worker_a,
    )
    assert replay.created is False
