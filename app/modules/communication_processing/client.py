"""HTTP client for Nipun's internal worker API (`/api/v1/internal/worker-jobs/*`).

The only way this module talks to the rest of the system: no direct
PostgreSQL/Neo4j/Redis/MinIO access, ever. Every request carries
`Authorization: Bearer <WORKER_TOKEN>` -- this worker process's own
per-worker credential, verified against `require_worker_principal` and its
`allowed_processor_names` scope (see
`docs/architecture/worker-identity-and-security.md`). Result submission and
input resolution additionally carry the per-job `X-Claim-Token` header,
exactly as `internal_api.py::submit_result`/`get_worker_job_input` expect.

Mirrors `app.modules.structured_processing.client` exactly (down to the
wire format) rather than importing it: this module owns its own copy, using
its own `errors.py`/`limits.py`, matching the existing repository-wide
per-module-ownership convention.

Never logs the worker token, a claim token, or a raw response body --
every log call below passes only `job_id`s, processor names, and status
codes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote
from uuid import UUID

import httpx
import structlog

from app.contracts.observation_batch import ObservationBatchReceiptV1, ObservationBatchSubmissionV1
from app.contracts.worker import WorkerJobV1, WorkerResultV1
from app.modules.communication_processing.errors import (
    InputResolutionUnavailableError,
    WorkerApiError,
    WorkerAuthenticationError,
)
from app.modules.communication_processing.input_resolver import ResolvedInput
from app.modules.communication_processing.limits import MAX_INPUT_BYTES

logger = structlog.get_logger(__name__)

_CLAIM_TOKEN_HEADER = "X-Claim-Token"
_SHA256_HEADER = "X-TraceX-Evidence-SHA256"
_CLAIM_PATH = "/api/v1/internal/worker-jobs/claim"
_INPUT_PATH_TEMPLATE = "/api/v1/internal/worker-jobs/{job_id}/input"

#: RFC 6266 `filename*=UTF-8''<percent-encoded>` -- preferred when present
#: (correct for any non-ASCII original filename); `filename="..."` is the
#: ASCII-safe fallback always sent alongside it.
_FILENAME_STAR_RE = re.compile(r"filename\*=UTF-8''([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename="([^"]*)"')


def _parse_content_disposition_filename(header_value: str) -> str:
    star_match = _FILENAME_STAR_RE.search(header_value)
    if star_match:
        return unquote(star_match.group(1))
    plain_match = _FILENAME_RE.search(header_value)
    if plain_match:
        return plain_match.group(1)
    return ""


def _result_path(job_id: UUID) -> str:
    return f"/api/v1/internal/worker-jobs/{job_id}/result"


def _observations_path(job_id: UUID) -> str:
    return f"/api/v1/internal/worker-jobs/{job_id}/observations"


def _renew_path(job_id: UUID) -> str:
    return f"/api/v1/internal/worker-jobs/{job_id}/renew"


@dataclass(frozen=True)
class ClaimResult:
    """Mirrors `evidence_lifecycle.schemas.ClaimResponse`'s shape without importing it.

    `job`/`claim_token`/`lease_expires_at` are all `None` when nothing is
    eligible right now -- a normal, safe outcome, not an error.
    """

    job: WorkerJobV1 | None
    claim_token: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True)
class SubmitResultAck:
    """Mirrors `evidence_lifecycle.schemas.ResultAcknowledgement`'s shape without importing it."""

    job_id: UUID
    status: str
    result_id: UUID
    observation_count: int
    observation_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class RenewAck:
    """Mirrors `evidence_lifecycle.schemas.RenewLeaseResponse`'s shape without importing it."""

    job_id: UUID
    lease_expires_at: datetime


class WorkerApiClient:
    """Thin, typed wrapper over `/api/v1/internal/worker-jobs/*`.

    Owns one `httpx.Client` (sync -- the worker CLI is a one-shot script,
    no event loop needed). `base_url`/`worker_token` are injected, never
    read from `Settings` directly inside this class, so tests can point it
    at a local test transport without touching real configuration.
    """

    def __init__(
        self,
        *,
        base_url: str,
        worker_token: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {worker_token}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WorkerApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def claim(self, *, processor_name: str, processor_version: str) -> ClaimResult:
        logger.info(
            "worker.client.claim_attempted",
            processor_name=processor_name,
            processor_version=processor_version,
        )
        response = self._post_safely(
            _CLAIM_PATH,
            json={"processor_name": processor_name, "processor_version": processor_version},
        )
        _raise_for_auth_failure(response)
        if response.status_code != httpx.codes.OK:
            raise WorkerApiError(f"claim request failed: HTTP {response.status_code}")
        body = response.json()
        job = WorkerJobV1.model_validate(body["job"]) if body.get("job") is not None else None
        return ClaimResult(
            job=job,
            claim_token=body.get("claim_token"),
            lease_expires_at=_parse_optional_datetime(body.get("lease_expires_at")),
        )

    def submit_result(
        self, *, job_id: UUID, claim_token: str, result: WorkerResultV1
    ) -> SubmitResultAck:
        logger.info("worker.client.submit_attempted", job_id=str(job_id))
        response = self._post_safely(
            _result_path(job_id),
            content=result.model_dump_json(),
            headers={"Content-Type": "application/json", _CLAIM_TOKEN_HEADER: claim_token},
        )
        _raise_for_auth_failure(response)
        if response.status_code == httpx.codes.CONFLICT:
            raise WorkerApiError(f"result submission conflict for job {job_id}")
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
            raise WorkerApiError(f"result submission rejected for job {job_id}: validation failed")
        if response.status_code != httpx.codes.OK:
            raise WorkerApiError(f"result submission failed: HTTP {response.status_code}")
        body = response.json()
        return SubmitResultAck(
            job_id=UUID(body["job_id"]),
            status=body["status"],
            result_id=UUID(body["result_id"]),
            observation_count=body["observation_count"],
            observation_ids=tuple(UUID(o) for o in body["observation_ids"]),
        )

    def submit_batch(
        self, *, job_id: UUID, claim_token: str, submission: ObservationBatchSubmissionV1
    ) -> ObservationBatchReceiptV1:
        """Submit one partial observation micro-batch via Nipun's `/observations` endpoint.

        Idempotent on the caller's side too: retrying the exact same
        `submission` (same `batch_id`/`idempotency_key`/content) after a
        transport failure is always safe -- the server replays rather than
        duplicates (see `app/contracts/observation_batch.py`).
        """
        logger.info(
            "worker.client.submit_batch_attempted",
            job_id=str(job_id),
            batch_id=submission.batch_id,
        )
        response = self._post_safely(
            _observations_path(job_id),
            content=submission.model_dump_json(),
            headers={"Content-Type": "application/json", _CLAIM_TOKEN_HEADER: claim_token},
        )
        _raise_for_auth_failure(response)
        if response.status_code == httpx.codes.CONFLICT:
            raise WorkerApiError(f"batch submission conflict for job {job_id}")
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
            raise WorkerApiError(f"batch submission rejected for job {job_id}: validation failed")
        if response.status_code != httpx.codes.OK:
            raise WorkerApiError(f"batch submission failed: HTTP {response.status_code}")
        return ObservationBatchReceiptV1.model_validate(response.json())

    def renew_lease(self, job_id: UUID, *, claim_token: str) -> RenewAck:
        """Extend a currently-claimed job's lease via Aditya's `/renew` endpoint.

        Used between micro-batch submissions for a long-running audio/chat
        job so its lease never expires mid-processing -- the same
        server-enforced ceiling (`WORKER_LEASE_MAX_SECONDS`) and
        current-claim-owner-only authorization documented in
        `docs/architecture/worker-job-lifecycle.md` apply exactly as they
        do to any other caller of this endpoint.
        """
        logger.info("worker.client.renew_attempted", job_id=str(job_id))
        response = self._post_safely(
            _renew_path(job_id), headers={_CLAIM_TOKEN_HEADER: claim_token}
        )
        _raise_for_auth_failure(response)
        if response.status_code != httpx.codes.OK:
            raise WorkerApiError(f"lease renewal failed: HTTP {response.status_code}")
        body = response.json()
        return RenewAck(
            job_id=UUID(body["job_id"]),
            lease_expires_at=datetime.fromisoformat(body["lease_expires_at"]),
        )

    def fetch_input(self, job_id: UUID, *, claim_token: str) -> ResolvedInput:
        """Fetch a claimed job's evidence bytes through Nipun's claim-token-bound input endpoint.

        `InputResolutionUnavailableError` on a `404` is a defensive path
        (e.g. against an older API build without this route) -- see
        docs/architecture/evidence-lifecycle.md's "Worker evidence
        delivery" for the endpoint's full shape.
        """
        try:
            response = self._client.get(
                _INPUT_PATH_TEMPLATE.format(job_id=job_id),
                headers={_CLAIM_TOKEN_HEADER: claim_token},
            )
        except httpx.HTTPError as exc:
            raise WorkerApiError(f"input request failed: {type(exc).__name__}") from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise InputResolutionUnavailableError(
                "no input-access endpoint is available on the internal worker API "
                "yet -- see docs/architecture/evidence-lifecycle.md"
            )
        _raise_for_auth_failure(response)
        if response.status_code != httpx.codes.OK:
            raise WorkerApiError(f"input request failed: HTTP {response.status_code}")

        if len(response.content) > MAX_INPUT_BYTES:
            # A worker-side bound independent of `process_job`'s own,
            # profile-specific checks (which only run after this call
            # returns): closes the gap where an oversized response would
            # otherwise sit fully buffered in this process first.
            raise WorkerApiError(
                f"resolved evidence exceeds the {MAX_INPUT_BYTES}-byte worker-side limit"
            )

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        filename = _parse_content_disposition_filename(
            response.headers.get("content-disposition", "")
        )
        expected_sha256 = response.headers.get(_SHA256_HEADER) or None
        return ResolvedInput(
            content_type=content_type,
            original_filename=filename,
            data=response.content,
            expected_sha256=expected_sha256,
        )

    def _post_safely(self, path: str, **kwargs: object) -> httpx.Response:
        try:
            return self._client.post(path, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise WorkerApiError(f"request to {path} failed: {type(exc).__name__}") from exc


def _raise_for_auth_failure(response: httpx.Response) -> None:
    if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.SERVICE_UNAVAILABLE):
        raise WorkerAuthenticationError(
            f"internal worker API rejected this request: HTTP {response.status_code}"
        )


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):  # pragma: no cover - defensive, API always sends str|null
        raise WorkerApiError("internal worker API returned a malformed timestamp")
    return datetime.fromisoformat(value)
