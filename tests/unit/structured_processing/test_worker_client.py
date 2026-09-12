"""Scenario 15 (client half), 17: `WorkerApiClient` against a fake internal-API transport.

Uses `httpx.MockTransport` -- a real `httpx.Client` request/response cycle,
no real server, no `FastAPI`/`uvicorn` needed. This is the unit-level proof
that the client speaks Nipun's exact wire format (request bodies, header
names, status-code handling); `tests/integration/structured_processing/
test_worker_live.py` proves the same client against the real running API.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.contracts.observation_batch import BatchAcceptanceStatus
from app.contracts.worker import WorkerStatus
from app.modules.structured_processing.client import WorkerApiClient
from app.modules.structured_processing.errors import (
    InputResolutionUnavailableError,
    WorkerApiError,
    WorkerAuthenticationError,
)
from tests.fixtures.factories import (
    make_observation_batch_submission,
    make_worker_job,
    make_worker_result,
)

_SHARED_SECRET = "unit-test-shared-secret"


def _client(handler: object) -> WorkerApiClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return WorkerApiClient(
        base_url="http://internal-api.test", shared_secret=_SHARED_SECRET, transport=transport
    )


# --- claim ---------------------------------------------------------------


def test_claim_returns_job_when_available() -> None:
    job = make_worker_job(processor_name="cdr_generic_v1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/worker-jobs/claim"
        assert request.headers["authorization"] == f"Bearer {_SHARED_SECRET}"
        body = json.loads(request.content)
        assert body == {"processor_name": "cdr_generic_v1", "processor_version": "1.0.0"}
        return httpx.Response(
            200,
            json={
                "job": json.loads(job.model_dump_json()),
                "claim_token": "opaque-claim-token",
                "lease_expires_at": "2026-01-01T12:05:00Z",
            },
        )

    result = _client(handler).claim(processor_name="cdr_generic_v1", processor_version="1.0.0")
    assert result.job == job
    assert result.claim_token == "opaque-claim-token"
    assert result.lease_expires_at is not None


def test_claim_returns_no_work_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, json={"job": None, "claim_token": None, "lease_expires_at": None}
        )

    result = _client(handler).claim(processor_name="cdr_generic_v1", processor_version="1.0.0")
    assert result.job is None
    assert result.claim_token is None


def test_claim_raises_auth_error_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).claim(processor_name="cdr_generic_v1", processor_version="1.0.0")


def test_claim_raises_auth_error_when_worker_integration_unconfigured_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, json={"error": {"code": "unavailable", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).claim(processor_name="cdr_generic_v1", processor_version="1.0.0")


# --- submit_result ----------------------------------------------------------


def test_submit_result_success() -> None:
    job_id = uuid4()
    result = make_worker_result(job_id=job_id, status=WorkerStatus.SUCCEEDED, error=None)
    observation_id = result.observations[0].observation_id

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/internal/worker-jobs/{job_id}/result"
        assert request.headers["x-claim-token"] == "tok-123"
        submitted = json.loads(request.content)
        assert submitted["job_id"] == str(job_id)
        return httpx.Response(
            200,
            json={
                "job_id": str(job_id),
                "status": "succeeded",
                "result_id": str(uuid4()),
                "observation_count": 1,
                "observation_ids": [str(observation_id)],
            },
        )

    ack = _client(handler).submit_result(job_id=job_id, claim_token="tok-123", result=result)
    assert ack.status == "succeeded"
    assert ack.observation_ids == (observation_id,)


def test_submit_result_conflict_raises_safely() -> None:
    job_id = uuid4()
    result = make_worker_result(job_id=job_id, error=None)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(409, json={"error": {"code": "conflict", "message": "no"}})

    with pytest.raises(WorkerApiError):
        _client(handler).submit_result(job_id=job_id, claim_token="tok-123", result=result)


def test_submit_result_validation_error_raises_safely() -> None:
    job_id = uuid4()
    result = make_worker_result(job_id=job_id, error=None)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(422, json={"error": {"code": "validation_error", "message": "no"}})

    with pytest.raises(WorkerApiError):
        _client(handler).submit_result(job_id=job_id, claim_token="tok-123", result=result)


def test_submit_result_wrong_claim_token_raises_auth_error() -> None:
    job_id = uuid4()
    result = make_worker_result(job_id=job_id, error=None)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).submit_result(job_id=job_id, claim_token="wrong", result=result)


# --- submit_batch (Phase 3 -- Jasraj) ---------------------------------------


def test_submit_batch_success() -> None:
    submission = make_observation_batch_submission()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/internal/worker-jobs/{submission.job_id}/observations"
        assert request.headers["x-claim-token"] == "tok-batch"
        submitted = json.loads(request.content)
        assert submitted["batch_id"] == submission.batch_id
        return httpx.Response(
            200,
            json={
                "schema_version": "v1",
                "job_id": str(submission.job_id),
                "batch_id": submission.batch_id,
                "status": "accepted",
                "accepted_observation_count": 1,
                "progress": json.loads(submission.progress.model_dump_json()),
                "request_id": "req-1",
            },
        )

    receipt = _client(handler).submit_batch(
        job_id=submission.job_id, claim_token="tok-batch", submission=submission
    )
    assert receipt.status is BatchAcceptanceStatus.ACCEPTED
    assert receipt.accepted_observation_count == 1


def test_submit_batch_conflict_raises_safely() -> None:
    submission = make_observation_batch_submission()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(409, json={"error": {"code": "conflict", "message": "no"}})

    with pytest.raises(WorkerApiError):
        _client(handler).submit_batch(
            job_id=submission.job_id, claim_token="tok-batch", submission=submission
        )


def test_submit_batch_wrong_claim_token_raises_auth_error() -> None:
    submission = make_observation_batch_submission()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).submit_batch(
            job_id=submission.job_id, claim_token="wrong", submission=submission
        )


def test_submit_batch_never_logs_the_claim_token(caplog: pytest.LogCaptureFixture) -> None:
    submission = make_observation_batch_submission()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            json={
                "schema_version": "v1",
                "job_id": str(submission.job_id),
                "batch_id": submission.batch_id,
                "status": "accepted",
                "accepted_observation_count": 1,
                "progress": None,
                "request_id": None,
            },
        )

    with caplog.at_level("DEBUG"):
        _client(handler).submit_batch(
            job_id=submission.job_id, claim_token="super-secret-claim-token", submission=submission
        )
    assert "super-secret-claim-token" not in caplog.text


# --- renew_lease (Phase 3 -- Jasraj) -----------------------------------------


def test_renew_lease_success() -> None:
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/internal/worker-jobs/{job_id}/renew"
        assert request.headers["x-claim-token"] == "tok-renew"
        return httpx.Response(
            200, json={"job_id": str(job_id), "lease_expires_at": "2026-01-01T12:10:00Z"}
        )

    ack = _client(handler).renew_lease(job_id, claim_token="tok-renew")
    assert ack.job_id == job_id


def test_renew_lease_raises_auth_error_on_401() -> None:
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).renew_lease(job_id, claim_token="stale")


# --- fetch_input (the proposed, currently-unavailable endpoint) -------------


def test_fetch_input_success_when_endpoint_exists() -> None:
    job_id = uuid4()
    sha256 = "b" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/internal/worker-jobs/{job_id}/input"
        assert request.headers["x-claim-token"] == "tok-123"
        return httpx.Response(
            200,
            content=b"hello world",
            headers={
                "content-type": "text/plain",
                "content-disposition": (
                    "attachment; filename=\"notes.txt\"; filename*=UTF-8''notes.txt"
                ),
                "x-tracex-evidence-sha256": sha256,
            },
        )

    resolved = _client(handler).fetch_input(job_id, claim_token="tok-123")
    assert resolved.data == b"hello world"
    assert resolved.content_type == "text/plain"
    assert resolved.original_filename == "notes.txt"
    assert resolved.expected_sha256 == sha256


def test_fetch_input_rejects_oversized_response() -> None:
    job_id = uuid4()
    oversized = b"x" * (50 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=oversized, headers={"content-type": "text/plain"})

    with pytest.raises(WorkerApiError):
        _client(handler).fetch_input(job_id, claim_token="tok-123")


def test_fetch_input_raises_unavailable_when_endpoint_missing() -> None:
    """The documented, current reality: the proposed endpoint doesn't exist yet (404)."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(404, json={"detail": "Not Found"})

    with pytest.raises(InputResolutionUnavailableError):
        _client(handler).fetch_input(uuid4(), claim_token="tok-123")


# --- secret safety ------------------------------------------------------------


def test_client_never_logs_the_shared_secret_or_claim_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, json={"job": None, "claim_token": None, "lease_expires_at": None}
        )

    with caplog.at_level("DEBUG"):
        _client(handler).claim(processor_name="cdr_generic_v1", processor_version="1.0.0")

    log_text = caplog.text
    assert _SHARED_SECRET not in log_text


def test_client_never_logs_claim_token_on_submit(caplog: pytest.LogCaptureFixture) -> None:
    job_id = uuid4()
    result = make_worker_result(job_id=job_id, error=None)
    secret_token = "super-secret-claim-token-value"

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            json={
                "job_id": str(job_id),
                "status": "succeeded",
                "result_id": str(uuid4()),
                "observation_count": 0,
                "observation_ids": [],
            },
        )

    with caplog.at_level("DEBUG"):
        _client(handler).submit_result(job_id=job_id, claim_token=secret_token, result=result)

    assert secret_token not in caplog.text
