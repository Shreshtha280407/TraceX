"""Scenarios 9-17: `EvidenceLifecycleService.submit_observation_batch` unit tests.

Reuses `tests.fixtures.factories.make_observation_batch_submission`/
`make_transformation_provenance`/`make_batch_progress` (the shared,
already-validated contract builders) rather than duplicating them, mirroring
`test_worker_result.py`'s conventions exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.observation_batch import BatchAcceptanceStatus
from app.modules.evidence_lifecycle.errors import (
    InvalidClaimTokenError,
    ObservationBatchConflictError,
    ObservationBatchValidationError,
)
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import (
    make_batch_progress,
    make_observation,
    make_observation_batch_submission,
    make_transformation_provenance,
    make_worker_result,
)


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


def _context(now: datetime | None = None) -> UploadContext:
    return UploadContext(now=now or datetime.now(UTC), request_id="req-test")


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


# --- Scenario 9: a valid multi-observation batch persists everything -------


async def test_valid_multi_observation_batch_persists_everything() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    obs1 = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    obs2 = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    transformation = make_transformation_provenance(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-1",
        output_observation_ids=[obs1.observation_id, obs2.observation_id],
    )
    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-1",
        idempotency_key="idem-1",
        observations=[obs1, obs2],
        transformations=[transformation],
        progress=make_batch_progress(batch_sequence=0),
    )

    outcome = await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )

    assert outcome.status is BatchAcceptanceStatus.ACCEPTED
    assert outcome.accepted_observation_count == 2
    assert len(repository.observation_batches) == 1
    assert len(repository.observations) == 2
    assert len(repository.transformations) == 1
    assert len(repository.progress_events) == 1
    assert len(repository.graph_projection_jobs) == 2
    stored_batch = next(iter(repository.observation_batches.values()))
    assert stored_batch.job_id == job.job_id
    assert stored_batch.observation_count == 2
    assert outcome.progress is not None
    assert outcome.progress.batch_sequence == 0


# --- Scenario 10: an invalid batch (wrong scope) persists nothing ----------


async def test_batch_outside_job_scope_persists_nothing() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=uuid4(),  # not this job's case
        evidence_id=job.evidence_id,
        batch_id="batch-1",
    )
    with pytest.raises(ObservationBatchValidationError):
        await service.submit_observation_batch(
            job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
        )
    assert repository.observation_batches == {}
    assert repository.observations == {}
    assert repository.transformations == {}
    assert repository.progress_events == {}
    assert repository.graph_projection_jobs == {}


async def test_batch_against_a_not_running_job_persists_nothing() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    # Mark the job terminal via a real result submission first.
    result = make_worker_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        observations=[],
        error=None,
    )
    await service.submit_result(
        job_id=job.job_id, claim_token=claim_token, result=result, context=_context()
    )

    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="a-brand-new-batch",
        idempotency_key="a-brand-new-key",
    )
    with pytest.raises(ObservationBatchValidationError):
        await service.submit_observation_batch(
            job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
        )
    assert repository.observation_batches == {}


# --- Scenario 11: identical retry returns the original receipt ------------


async def test_identical_retry_returns_original_receipt_without_duplicates() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )

    first = await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )
    second = await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )

    assert first.status is BatchAcceptanceStatus.ACCEPTED
    assert second.status is BatchAcceptanceStatus.REPLAYED
    assert second.accepted_observation_count == first.accepted_observation_count
    assert len(repository.observation_batches) == 1
    assert len(repository.observations) == 1


# --- Scenario 12: same idempotency key + changed payload is a conflict -----


async def test_same_batch_id_and_key_with_changed_payload_is_conflict() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )

    different_payload = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id=submission.batch_id,
        idempotency_key=submission.idempotency_key,
        progress=make_batch_progress(batch_sequence=0, units_completed=9),
    )
    with pytest.raises(ObservationBatchConflictError):
        await service.submit_observation_batch(
            job_id=job.job_id,
            claim_token=claim_token,
            submission=different_payload,
            context=_context(),
        )
    assert len(repository.observation_batches) == 1  # still just the original


async def test_same_idempotency_key_reused_for_a_different_batch_id_is_conflict() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )

    key_reuse = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="a-different-batch-id",
        idempotency_key=submission.idempotency_key,
        batch_sequence=1,
        progress=make_batch_progress(batch_sequence=1),
    )
    with pytest.raises(ObservationBatchConflictError):
        await service.submit_observation_batch(
            job_id=job.job_id, claim_token=claim_token, submission=key_reuse, context=_context()
        )
    assert len(repository.observation_batches) == 1


# --- Scenario 13: duplicate observation_id across batches is deterministic -


async def test_duplicate_observation_id_reused_in_a_different_batch_is_conflict() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    observation = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    first = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-1",
        idempotency_key="idem-1",
        observations=[observation],
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=first, context=_context()
    )

    reused = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-2",
        idempotency_key="idem-2",
        batch_sequence=1,
        observations=[observation],  # same observation_id, genuinely different batch
        progress=None,
    )
    with pytest.raises(ObservationBatchConflictError):
        await service.submit_observation_batch(
            job_id=job.job_id, claim_token=claim_token, submission=reused, context=_context()
        )
    assert len(repository.observations) == 1  # unchanged -- no second row, no duplicate
    assert len(repository.observation_batches) == 1  # the conflicting batch never persisted


# --- Scenario 14: replay enqueues no additional graph projection jobs ------


async def test_replay_creates_no_additional_graph_projection_jobs() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )

    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )
    assert len(repository.graph_projection_jobs) == 1

    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )
    assert len(repository.graph_projection_jobs) == 1  # unchanged


# --- Scenario 15: a failed transaction leaves no orphaned rows -------------


async def test_failed_persistence_leaves_no_orphaned_rows() -> None:
    class _BrokenRepository(FakeEvidenceLifecycleRepository):
        async def submit_observation_batch(self, **kwargs: object) -> None:  # type: ignore[override]
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
    submission = make_observation_batch_submission(
        job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id
    )

    with pytest.raises(RuntimeError):
        await service.submit_observation_batch(
            job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
        )

    assert repository.observation_batches == {}
    assert repository.observations == {}
    assert repository.transformations == {}
    assert repository.progress_events == {}
    assert repository.graph_projection_jobs == {}


# --- Scenario 16: progress ordering and regression rules --------------------


async def test_progress_events_remain_ordered_and_reject_regression_within_same_attempt() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)

    first = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-1",
        idempotency_key="idem-1",
        progress=make_batch_progress(batch_sequence=0, units_completed=1, observations_emitted=1),
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=first, context=_context()
    )

    second = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-2",
        idempotency_key="idem-2",
        batch_sequence=1,
        progress=make_batch_progress(batch_sequence=1, units_completed=5, observations_emitted=2),
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=second, context=_context()
    )

    latest = await repository.get_latest_progress_event(job.job_id)
    assert latest is not None
    assert latest.units_completed == 5
    stored = sorted(repository.progress_events.values(), key=lambda p: p.ordinal)
    assert [p.units_completed for p in stored] == [1, 5]  # insertion order preserved

    regressed = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-3",
        idempotency_key="idem-3",
        batch_sequence=2,
        progress=make_batch_progress(batch_sequence=2, units_completed=1, observations_emitted=1),
    )
    with pytest.raises(ObservationBatchValidationError):
        await service.submit_observation_batch(
            job_id=job.job_id, claim_token=claim_token, submission=regressed, context=_context()
        )
    assert len(repository.progress_events) == 2  # the regressed attempt never persisted


async def test_progress_may_reset_after_a_legitimate_reclaim_bumps_attempt() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)

    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        progress=make_batch_progress(batch_sequence=0, units_completed=8, observations_emitted=3),
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )

    # Simulate the lease expiring and a legitimate reclaim (attempt increments).
    expired_now = _context(now=datetime.now(UTC) + timedelta(seconds=120))
    reclaim = await service.claim_job(
        processor_name="cdr_generic_v1", processor_version="1.0.0", context=expired_now
    )
    assert reclaim.job is not None
    assert reclaim.job.attempt == job.attempt + 1
    new_claim_token = reclaim.claim_token
    assert new_claim_token is not None

    reset_progress = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-after-reclaim",
        idempotency_key="idem-after-reclaim",
        batch_sequence=0,
        progress=make_batch_progress(batch_sequence=0, units_completed=0, observations_emitted=0),
    )
    outcome = await service.submit_observation_batch(
        job_id=job.job_id,
        claim_token=new_claim_token,
        submission=reset_progress,
        context=expired_now,
    )
    assert outcome.status is BatchAcceptanceStatus.ACCEPTED  # reset allowed -- new attempt


async def test_stale_claim_token_cannot_submit_a_new_batch_after_reclaim() -> None:
    """The *old* claim token -- valid before the lease expired and the job was
    reclaimed by (in this case) a different worker -- can never submit a *new*
    batch afterward: the reclaim already overwrote `claim_token_hash`."""
    service, repository = _service()
    job, old_claim_token = await _claimed_job(service)

    expired_now = _context(now=datetime.now(UTC) + timedelta(seconds=120))
    reclaim = await service.claim_job(
        processor_name="cdr_generic_v1", processor_version="1.0.0", context=expired_now
    )
    assert reclaim.job is not None
    assert reclaim.claim_token != old_claim_token

    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-with-stale-token",
        idempotency_key="idem-with-stale-token",
    )
    with pytest.raises(InvalidClaimTokenError) as excinfo:
        await service.submit_observation_batch(
            job_id=job.job_id,
            claim_token=old_claim_token,
            submission=submission,
            context=expired_now,
        )
    assert excinfo.value.reason == "token_mismatch"
    assert repository.observation_batches == {}
    assert repository.observations == {}


# --- Scenario 17: provenance retrievable by case/evidence/job/observation --


async def test_transformation_provenance_retrievable_by_batch_linkage() -> None:
    service, repository = _service()
    job, claim_token = await _claimed_job(service)
    observation = make_observation(case_id=job.case_id, evidence_id=job.evidence_id)
    transformation = make_transformation_provenance(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-1",
        ordinal=0,
        output_observation_ids=[observation.observation_id],
    )
    submission = make_observation_batch_submission(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id="batch-1",
        observations=[observation],
        transformations=[transformation],
    )
    await service.submit_observation_batch(
        job_id=job.job_id, claim_token=claim_token, submission=submission, context=_context()
    )

    stored_batch = await repository.get_batch_by_job_and_batch_id(job.job_id, "batch-1")
    assert stored_batch is not None
    retrieved = await repository.list_transformations_for_batch(stored_batch.observation_batch_id)
    assert len(retrieved) == 1
    assert retrieved[0].transformation_id == transformation.transformation_id
    assert retrieved[0].job_id == job.job_id
    assert retrieved[0].case_id == job.case_id
    assert retrieved[0].evidence_id == job.evidence_id

    retrieved_observations = await repository.list_observations_for_batch(
        stored_batch.observation_batch_id
    )
    assert len(retrieved_observations) == 1
    assert retrieved_observations[0].observation_id == observation.observation_id
