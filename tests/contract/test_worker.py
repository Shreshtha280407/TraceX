"""Contract tests for WorkerJobV1 / WorkerResultV1 / WorkerProgressV1.

See docs/qa/test-matrix.md (CORE-CONTRACT-001).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.worker import WorkerError, WorkerJobV1, WorkerStatus
from tests.fixtures.factories import assert_roundtrips, make_worker_job, make_worker_result


def test_valid_worker_job_parses() -> None:
    job = make_worker_job()
    assert job.schema_version == "v1"


def test_worker_job_roundtrips() -> None:
    assert_roundtrips(make_worker_job())


def test_worker_job_unsupported_schema_version_is_rejected() -> None:
    payload = make_worker_job().model_dump(mode="json")
    payload["schema_version"] = "v2"
    with pytest.raises(ValidationError):
        WorkerJobV1(**payload)


@pytest.mark.parametrize(
    "bad_key",
    [
        "not-enough-segments",
        "case:evidence:proc",
        "case:evidence:proc:ver:extra",
        "case evidence:proc:ver",
    ],
)
def test_worker_job_rejects_malformed_idempotency_key(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        make_worker_job(idempotency_key=bad_key)


def test_worker_job_requires_attempt_at_least_one() -> None:
    with pytest.raises(ValidationError):
        make_worker_job(attempt=0)


def test_valid_worker_result_parses() -> None:
    result = make_worker_result()
    assert result.status == WorkerStatus.SUCCEEDED


def test_worker_result_roundtrips() -> None:
    assert_roundtrips(make_worker_result())


def test_worker_result_failed_without_error_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_worker_result(status=WorkerStatus.FAILED, error=None)


def test_worker_result_failed_with_error_is_valid() -> None:
    result = make_worker_result(
        status=WorkerStatus.FAILED,
        error=WorkerError(code="parse_error", message="Could not parse CDR file"),
        observations=[],
    )
    assert result.error is not None


def test_worker_result_succeeded_with_error_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_worker_result(
            status=WorkerStatus.SUCCEEDED,
            error=WorkerError(code="x", message="y"),
        )
