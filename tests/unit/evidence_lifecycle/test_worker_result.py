"""Scenarios 6-12, 14: `EvidenceLifecycleService.submit_result` unit tests.

Reuses `tests.fixtures.factories.make_worker_result`/`make_observation` (the
shared, already-validated contract builders) rather than duplicating them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.observation import ObservationV1
from app.contracts.worker import WorkerError, WorkerStatus
from app.modules.evidence_lifecycle.errors import (
    InvalidClaimTokenError,
    ResultConflictError,
    ResultValidationError,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import assert_roundtrips, make_observation, make_worker_result


def _service() -> tuple[EvidenceLifecycleService, FakeEvidenceLifecycleRepository]:
    repository = FakeEvidenceLifecycleRepository()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=60,
    )
    return service, repository


def _context() -> UploadContext:
    return UploadContext(now=datetime.now(UTC), request_id="req-test")


async def _claimed_job(service: EvidenceLifecycleService):
    case_id = uuid4()
    await service.upload_evidence(
        case_id=case_id,
        uploaded_by=uuid4(),
        source_type=SourceType.CDR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=b"a,b\n1,2\n", content_type="text/csv"),
        idempotency_key=None,
        context=_context(),
    )
    claim = await service.claim_job(
        processor_name="cdr_generic_v1", processor_version="1.0.0", context=_context()
    )
    assert claim.job is not None
    assert claim.claim_token is not None
    return claim.job, claim.claim_token


# --- Scenario 6-7: valid success result transitions the job + persists observations ---


async def test_valid_success_result_transitions_job_and_persists_observations() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    observation = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[observation],
        error=None,
    )

    outcome = await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
    )

    assert outcome.created is True
    assert outcome.status is WorkerStatus.SUCCEEDED
    assert outcome.observation_ids == (observation.observation_id,)
    assert repository.jobs[job.job_id].status is WorkerStatus.SUCCEEDED
    assert len(repository.observations) == 1
    stored_observation = repository.observations[observation.observation_id]
    assert stored_observation.case_id == job.case_id
    assert stored_observation.evidence_id == job.evidence_id


# --- Scenario 8: mismatched case/evidence/job is rejected -------------------


async def test_result_with_mismatched_job_id_is_rejected() -> None:
    service, _ = _service()
    job, claim_token = await _claimed_job(service)
    result = make_worker_result(
        job_id=uuid4(),  # not this job
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        error=None,
    )
    with pytest.raises(ResultValidationError):
        await service.submit_result(
            job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
        )


async def test_observation_outside_job_scope_is_rejected() -> None:
    service, _ = _service()
    job, claim_token = await _claimed_job(service)
    outside_observation = make_observation(case_id=uuid4(), evidence_id=job.evidence_id)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[outside_observation],
        error=None,
    )
    with pytest.raises(ResultValidationError):
        await service.submit_result(
            job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
        )


async def test_non_terminal_status_is_rejected() -> None:
    service, _ = _service()
    job, claim_token = await _claimed_job(service)
    # WorkerResultV1 itself allows constructing a "running" status result
    # (only status/error consistency is contract-enforced) -- rejecting a
    # non-terminal submission is this module's own scope rule.
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.RUNNING,
        observations=[],
        error=None,
        completed_at=None,
    )
    with pytest.raises(ResultValidationError):
        await service.submit_result(
            job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
        )


# --- Scenario 9: invalid/expired/wrong claim token is rejected --------------


async def test_wrong_claim_token_is_rejected() -> None:
    service, _ = _service()
    job, _claim_token = await _claimed_job(service)
    result = make_worker_result(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id, error=None
    )
    with pytest.raises(InvalidClaimTokenError):
        await service.submit_result(
            job_id=job.job_id, claim_token="not-the-real-token", result=result, context=_context()
        )


async def test_wrong_claim_token_is_rejected_even_against_an_already_terminal_job() -> None:
    """Security regression: caught via live-Compose smoke testing.

    The claim-token check must run *before* the idempotent-replay/conflict
    branch, not be skipped once a job is terminal -- otherwise a caller
    with a wrong (or no) token could retrieve a completed job's cached
    result just by resubmitting byte-identical content, without ever
    having held a valid token for it.
    """
    service, _ = _service()
    job, claim_token = await _claimed_job(service)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[],
        error=None,
    )
    await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
    )

    with pytest.raises(InvalidClaimTokenError):
        await service.submit_result(
            job_id=job.job_id, claim_token="wrong-token", result=result, context=_context()
        )


async def test_unclaimed_job_rejects_any_result() -> None:
    service, _ = _service()
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
    fake_job_id = uuid4()
    result = make_worker_result(job_id=fake_job_id, error=None)
    with pytest.raises(InvalidClaimTokenError):
        await service.submit_result(
            job_id=fake_job_id, claim_token="whatever", result=result, context=_context()
        )


# --- Scenario 10-11: idempotent duplicate vs. conflicting duplicate ---------


async def test_exact_duplicate_result_submission_is_idempotent() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[make_observation(case_id=job.case_id, evidence_id=job.evidence_id)],
        error=None,
    )

    first = await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
    )
    second = await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
    )

    assert first.created is True
    assert second.created is False
    assert second.result_id == first.result_id
    assert second.observation_ids == first.observation_ids
    assert len(repository.results) == 1
    assert len(repository.observations) == 1  # never duplicated


async def test_different_payload_for_completed_job_is_conflict() -> None:
    service, _ = _service()
    job, claim_token = await _claimed_job(service)
    first_result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[],
        error=None,
    )
    await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=first_result, context=_context()
    )

    different_result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        error=WorkerError(code="different_outcome", message="a different result", retryable=False),
    )
    with pytest.raises(ResultConflictError):
        await service.submit_result(
            job_id=job.job_id,
            claim_token=claim_token,
            result=different_result,
            context=_context(),
        )


# --- Scenario 12: failed persistence rolls back everything ------------------


async def test_failed_persistence_leaves_job_non_terminal() -> None:
    class _BrokenRepository(FakeEvidenceLifecycleRepository):
        async def submit_result(self, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("simulated connection drop")

    repository = _BrokenRepository()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=10 * 1024 * 1024,
        worker_lease_seconds=60,
    )
    job, claim_token = await _claimed_job(service)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        observations=[make_observation(case_id=job.case_id, evidence_id=job.evidence_id)],
        error=None,
    )

    with pytest.raises(RuntimeError):
        await service.submit_result(
            job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
        )

    assert repository.jobs[job.job_id].status is WorkerStatus.RUNNING  # never marked terminal
    assert not repository.results
    assert not repository.observations


# --- Scenario 14: canonical round trip --------------------------------------


async def test_worker_result_and_observations_round_trip_without_semantic_change() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    observation = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        observations=[observation],
        error=None,
    )
    assert_roundtrips(result)
    assert_roundtrips(observation)

    outcome = await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
    )

    stored_result = repository.results[outcome.result_id]
    reconstructed = stored_result.to_contract()
    assert reconstructed.status == result.status
    assert reconstructed.job_id == result.job_id
    assert len(reconstructed.observations) == len(result.observations)
    assert reconstructed.observations[0].observation_id == observation.observation_id

    stored_observation = repository.observations[observation.observation_id]
    reconstructed_observation = ObservationV1.model_validate(stored_observation.canonical_payload)
    assert reconstructed_observation == observation
