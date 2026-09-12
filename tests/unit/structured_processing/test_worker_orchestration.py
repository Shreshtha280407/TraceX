"""Scenarios 15-17: `run_once`/`main` orchestration against a fake `WorkerApiClient`.

The fake below duck-types `client.WorkerApiClient`'s public surface
(`claim`/`submit_result`/`close`) -- `run_once` never constructs a client
itself, it only calls methods on whatever is injected, so no HTTP or
`httpx.MockTransport` is needed here (that wire-level proof lives in
`test_worker_client.py`). This file proves the claim -> resolve -> parse ->
submit sequencing, the multi-processor claim loop, the no-job exit, the
input-resolution-gap `DEFERRED` path, and that a submit failure propagates
without leaking the claim token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.contracts.evidence import SourceType
from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerStatus
from app.modules.structured_processing.client import ClaimResult, RenewAck, SubmitResultAck
from app.modules.structured_processing.errors import InputResolutionUnavailableError, WorkerApiError
from app.modules.structured_processing.input_resolver import ResolvedInput, StaticInputResolver
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FIR_REPORT_TEXT_V1,
)
from app.modules.structured_processing.worker import (
    CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE,
    SUPPORTED_PROCESSORS,
    RunOnceOutcome,
    main,
    run_once,
)
from tests.fixtures.factories import make_worker_job


@dataclass
class _FakeClient:
    """Duck-types `WorkerApiClient`'s `claim`/`submit_result`/`submit_batch`/`renew_lease`."""

    claim_responses: list[ClaimResult]
    submit_ack: SubmitResultAck | None = None
    submit_exception: Exception | None = None
    submit_calls: list[tuple[UUID, str, object]] = field(default_factory=list)
    batch_calls: list[ObservationBatchSubmissionV1] = field(default_factory=list)
    renew_calls: int = 0
    _claim_calls: int = 0

    def claim(self, *, processor_name: str, processor_version: str) -> ClaimResult:  # noqa: ARG002
        response = self.claim_responses[self._claim_calls]
        self._claim_calls += 1
        return response

    def submit_result(self, *, job_id: UUID, claim_token: str, result: object) -> SubmitResultAck:
        self.submit_calls.append((job_id, claim_token, result))
        if self.submit_exception is not None:
            raise self.submit_exception
        assert self.submit_ack is not None
        return self.submit_ack

    def submit_batch(
        self, *, job_id: UUID, claim_token: str, submission: ObservationBatchSubmissionV1
    ) -> ObservationBatchReceiptV1:
        self.batch_calls.append(submission)
        return ObservationBatchReceiptV1(
            job_id=job_id,
            batch_id=submission.batch_id,
            status=BatchAcceptanceStatus.ACCEPTED,
            accepted_observation_count=len(submission.observations),
            progress=submission.progress,
            request_id=None,
        )

    def renew_lease(self, job_id: UUID, *, claim_token: str) -> RenewAck:  # noqa: ARG002
        self.renew_calls += 1
        return RenewAck(job_id=job_id, lease_expires_at=datetime.now(UTC))

    def close(self) -> None:
        pass


@dataclass
class _RaisingResolver:
    def resolve(self, job: object, *, claim_token: str) -> ResolvedInput:  # noqa: ARG002
        raise InputResolutionUnavailableError(
            "no input-access endpoint is available on the internal worker API yet"
        )


def _ack(job_id: UUID, *, status: str = "succeeded") -> SubmitResultAck:
    return SubmitResultAck(
        job_id=job_id, status=status, result_id=uuid4(), observation_count=0, observation_ids=()
    )


def test_run_once_happy_path_claims_resolves_parses_and_submits() -> None:
    job = make_worker_job(
        source_type=SourceType.DOCUMENT,
        processor_name=FIR_REPORT_TEXT_V1.name,
        processor_version=FIR_REPORT_TEXT_V1.version,
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="text/plain", original_filename="notes.txt", data=b"plain notes, no labels"
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.claimed is True
    assert outcome.job_id == job.job_id
    assert outcome.result_status == "succeeded"
    assert len(client.submit_calls) == 1
    submitted_job_id, submitted_token, submitted_result = client.submit_calls[0]
    assert submitted_job_id == job.job_id
    assert submitted_token == "tok-abc"
    assert submitted_result.status is WorkerStatus.SUCCEEDED  # type: ignore[attr-defined]


def test_run_once_tries_each_processor_until_one_has_work() -> None:
    job = make_worker_job(
        source_type=SourceType.CDR,
        processor_name=CDR_GENERIC_V1.name,
        processor_version=CDR_GENERIC_V1.version,
    )
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None),
            ClaimResult(job=job, claim_token="tok-cdr", lease_expires_at=None),
        ],
        submit_ack=_ack(job.job_id),
    )
    csv_body = (
        b"caller_number,callee_number,timestamp,duration_seconds\n"
        b"+911111,+912222,2026-01-01T10:00:00Z,60\n"
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="text/csv", original_filename="cdr.csv", data=csv_body)
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert client._claim_calls == 2  # first processor had no job, second did
    assert outcome.claimed is True
    assert outcome.result_status == "succeeded"


def test_run_once_returns_claimed_false_when_no_processor_has_work() -> None:
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None)
            for _ in SUPPORTED_PROCESSORS
        ]
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="text/plain", original_filename="x.txt", data=b"")
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome == RunOnceOutcome(claimed=False, job_id=None, result_status=None)
    assert client._claim_calls == len(SUPPORTED_PROCESSORS)


def test_run_once_submits_deferred_when_input_resolution_unavailable() -> None:
    job = make_worker_job(
        source_type=SourceType.DOCUMENT,
        processor_name=FIR_REPORT_TEXT_V1.name,
        processor_version=FIR_REPORT_TEXT_V1.version,
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-x", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="deferred"),
    )

    outcome = run_once(client=client, input_resolver=_RaisingResolver())

    assert outcome.claimed is True
    assert outcome.result_status == "deferred"
    assert outcome.deferred_reason is not None
    assert len(client.submit_calls) == 1
    _, submitted_token, submitted_result = client.submit_calls[0]
    assert submitted_token == "tok-x"
    assert submitted_result.status is WorkerStatus.DEFERRED  # type: ignore[attr-defined]
    assert submitted_result.checkpoint == CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE  # type: ignore[attr-defined]
    assert submitted_result.observations == []  # type: ignore[attr-defined]


def test_run_once_submits_failed_when_resolved_bytes_fail_sha256_check() -> None:
    """Scenario 12: a wrong SHA-256 is detected safely before parsing ever runs."""
    job = make_worker_job(
        source_type=SourceType.DOCUMENT,
        processor_name=FIR_REPORT_TEXT_V1.name,
        processor_version=FIR_REPORT_TEXT_V1.version,
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-x", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="failed"),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="text/plain",
            original_filename="notes.txt",
            data=b"text that does not match the claimed hash",
            expected_sha256="0" * 64,  # deliberately wrong
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "failed"
    assert len(client.submit_calls) == 1
    _, _, submitted_result = client.submit_calls[0]
    assert submitted_result.status is WorkerStatus.FAILED  # type: ignore[attr-defined]
    assert submitted_result.error.code == "evidence_integrity_mismatch"  # type: ignore[attr-defined]
    assert submitted_result.observations == []  # type: ignore[attr-defined]


def test_run_once_submit_failure_propagates_without_leaking_claim_token() -> None:
    job = make_worker_job(
        source_type=SourceType.DOCUMENT,
        processor_name=FIR_REPORT_TEXT_V1.name,
        processor_version=FIR_REPORT_TEXT_V1.version,
    )
    secret_token = "super-secret-claim-token"  # noqa: S105
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token=secret_token, lease_expires_at=None)],
        submit_exception=WorkerApiError("result submission rejected"),
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="text/plain", original_filename="notes.txt", data=b"text")
    )

    with pytest.raises(WorkerApiError) as exc_info:
        run_once(client=client, input_resolver=resolver)

    assert secret_token not in str(exc_info.value)


# --- main() -------------------------------------------------------------


def test_main_returns_1_when_worker_token_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_TOKEN", "")
    assert main(["--once"]) == 1


def test_run_once_never_logs_the_claim_token(caplog: pytest.LogCaptureFixture) -> None:
    job = make_worker_job(
        source_type=SourceType.DOCUMENT,
        processor_name=FIR_REPORT_TEXT_V1.name,
        processor_version=FIR_REPORT_TEXT_V1.version,
    )
    secret_token = "super-secret-claim-token-for-logging-check"  # noqa: S105
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token=secret_token, lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="text/plain", original_filename="notes.txt", data=b"text")
    )

    with caplog.at_level("DEBUG"):
        run_once(client=client, input_resolver=resolver)

    assert secret_token not in caplog.text


def test_main_returns_0_and_exits_cleanly_when_no_job_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None)
            for _ in SUPPORTED_PROCESSORS
        ]
    )
    monkeypatch.setattr(
        "app.modules.structured_processing.worker._build_client",
        lambda settings: client,  # noqa: ARG005
    )

    assert main(["--once"]) == 0
    assert client._claim_calls == len(SUPPORTED_PROCESSORS)
