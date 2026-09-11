"""`WorkerApiClient` against a fake internal-API transport (`httpx.MockTransport`).

Uses `httpx.MockTransport` -- a real `httpx.Client` request/response cycle,
no real server, no `FastAPI`/`uvicorn` needed. This is the unit-level proof
that the client speaks Nipun's exact wire format (request bodies, header
names, status-code handling); `tests/integration/media_processing/
test_media_worker_live.py` proves the same client against the real running
API. Mirrors `tests/unit/communication_processing/test_communication_worker_client.py`
exactly.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerStatus
from app.modules.media_processing.client import WorkerApiClient
from app.modules.media_processing.errors import (
    InputResolutionUnavailableError,
    WorkerApiError,
    WorkerAuthenticationError,
)
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
from tests.fixtures.factories import make_worker_result
from tests.fixtures.media_processing.factory import make_evidence_and_job

_WORKER_TOKEN = "unit-test-media-worker-token"  # noqa: S105


def _client(handler: object) -> WorkerApiClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return WorkerApiClient(
        base_url="http://internal-api.test", worker_token=_WORKER_TOKEN, transport=transport
    )


# --- claim -------------------------------------------------------------------


def test_claim_returns_job_when_available() -> None:
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name="media_metadata_v1",
        source_type=SourceType.IMAGE,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/worker-jobs/claim"
        assert request.headers["authorization"] == f"Bearer {_WORKER_TOKEN}"
        body = json.loads(request.content)
        assert body == {"processor_name": "media_metadata_v1", "processor_version": "1.0.0"}
        return httpx.Response(
            200,
            json={
                "job": json.loads(job.model_dump_json()),
                "claim_token": "opaque-claim-token",
                "lease_expires_at": "2026-01-01T12:05:00Z",
            },
        )

    result = _client(handler).claim(processor_name="media_metadata_v1", processor_version="1.0.0")
    assert result.job == job
    assert result.claim_token == "opaque-claim-token"
    assert result.lease_expires_at is not None


def test_claim_returns_no_work_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, json={"job": None, "claim_token": None, "lease_expires_at": None}
        )

    result = _client(handler).claim(processor_name="media_metadata_v1", processor_version="1.0.0")
    assert result.job is None
    assert result.claim_token is None


def test_claim_raises_auth_error_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).claim(processor_name="media_metadata_v1", processor_version="1.0.0")


def test_claim_raises_api_error_on_403_scope_denied() -> None:
    """A processor outside this worker's `allowed_processor_names` scope -- e.g. a
    worker not scoped for `media_detection_v1` (never routed to live anyway, see
    `worker.SUPPORTED_PROCESSORS`'s docstring) trying to claim it regardless."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(403, json={"error": {"code": "forbidden", "message": "no"}})

    with pytest.raises(WorkerApiError):
        _client(handler).claim(processor_name="media_detection_v1", processor_version="1.0.0")


def test_claim_raises_auth_error_when_worker_integration_unconfigured_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, json={"error": {"code": "unavailable", "message": "no"}})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).claim(processor_name="media_metadata_v1", processor_version="1.0.0")


# --- submit_result -------------------------------------------------------------


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


# --- fetch_input ---------------------------------------------------------------


def test_fetch_input_success() -> None:
    job_id = uuid4()
    sha256 = "b" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/internal/worker-jobs/{job_id}/input"
        assert request.headers["x-claim-token"] == "tok-123"
        return httpx.Response(
            200,
            content=b"synthetic png bytes",
            headers={
                "content-type": "image/png",
                "content-disposition": "attachment; filename=\"a.png\"; filename*=UTF-8''a.png",
                "x-tracex-evidence-sha256": sha256,
            },
        )

    resolved = _client(handler).fetch_input(job_id, claim_token="tok-123")
    assert resolved.data == b"synthetic png bytes"
    assert resolved.content_type == "image/png"
    assert resolved.original_filename == "a.png"
    assert resolved.expected_sha256 == sha256


def test_fetch_input_rejects_oversized_response() -> None:
    job_id = uuid4()
    oversized = b"x" * (DEFAULT_MEDIA_LIMITS.max_input_bytes + 1)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, content=oversized, headers={"content-type": "image/png"})

    with pytest.raises(WorkerApiError):
        _client(handler).fetch_input(job_id, claim_token="tok-123")


def test_fetch_input_raises_unavailable_when_endpoint_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(404, json={"detail": "Not Found"})

    with pytest.raises(InputResolutionUnavailableError):
        _client(handler).fetch_input(uuid4(), claim_token="tok-123")


# --- renew (lease heartbeat) ------------------------------------------------------


def test_renew_success_returns_new_lease_expiry() -> None:
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/v1/internal/worker-jobs/{job_id}/renew"
        assert request.headers["x-claim-token"] == "tok-123"
        return httpx.Response(
            200,
            json={"job_id": str(job_id), "lease_expires_at": "2026-01-01T13:00:00+00:00"},
        )

    new_lease = _client(handler).renew(job_id, claim_token="tok-123")
    assert new_lease.isoformat() == "2026-01-01T13:00:00+00:00"


def test_renew_raises_auth_error_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(401, json={"detail": "invalid claim token"})

    with pytest.raises(WorkerAuthenticationError):
        _client(handler).renew(uuid4(), claim_token="wrong-token")


def test_renew_raises_api_error_on_unexpected_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, json={"detail": "internal error"})

    with pytest.raises(WorkerApiError):
        _client(handler).renew(uuid4(), claim_token="tok-123")


def test_renew_never_logs_the_claim_token(caplog: pytest.LogCaptureFixture) -> None:
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, json={"job_id": str(job_id), "lease_expires_at": "2026-01-01T13:00:00+00:00"}
        )

    _client(handler).renew(job_id, claim_token="super-secret-claim-token")
    assert "super-secret-claim-token" not in caplog.text


# --- secret safety ---------------------------------------------------------------


def test_client_never_logs_the_worker_token_or_claim_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, json={"job": None, "claim_token": None, "lease_expires_at": None}
        )

    with caplog.at_level("DEBUG"):
        _client(handler).claim(processor_name="media_metadata_v1", processor_version="1.0.0")

    assert _WORKER_TOKEN not in caplog.text


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
