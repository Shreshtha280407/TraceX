"""Scenario 19: the worker's HTTP client against a real running API server.

Self-skips (never fabricates success) whenever any of the following is
true, each checked explicitly:
  - no `.env` at the repo root
  - the live API server isn't reachable at `WORKER_API_BASE_URL`
  - `WORKER_SHARED_SECRET` isn't configured in that live environment

This test deliberately does *not* attempt the full claim -> parse ->
SUCCEEDED path: doing so would require seeding a real queued job through
the case/auth/evidence-upload flow (`access_control` + `evidence_lifecycle`,
both outside this module's ownership) purely to exercise this worker's HTTP
layer -- which is already proven at the wire level by
`tests/unit/structured_processing/test_worker_client.py`
(`httpx.MockTransport`) and at the orchestration level by
`tests/unit/structured_processing/test_worker_orchestration.py`. What this
test proves against the real running server, without fabrication:

  1. `claim()` reaches the real internal API and gets back a real "no work"
     response (nothing seeded a queued job for this suite to find).
  2. `fetch_input()` against the real server genuinely raises
     `InputResolutionUnavailableError` -- the documented input-access gap
     (see `docs/architecture/structured-processing-worker.md`) is real
     against live infrastructure, not just assumed from reading the router.

The test always ends in an explicit `pytest.skip` (never a plain pass)
naming the exact blocked command: a full `SUCCEEDED`-parse live run
(`uv run python -m app.modules.structured_processing.worker --once`
against a stack with a real queued job) cannot complete honestly until
`GET /api/v1/internal/worker-jobs/{job_id}/input` exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from dotenv import dotenv_values

from app.core.config import Settings
from app.modules.structured_processing.client import WorkerApiClient
from app.modules.structured_processing.errors import InputResolutionUnavailableError

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_worker_client_against_real_running_api() -> None:
    settings = _live_settings()
    base_url = settings.worker_api_base_url
    try:
        httpx.get(f"{base_url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server not reachable at {base_url}: {type(exc).__name__}; "
            "start it via `docker compose up --build -d` to run this test"
        )

    secret = settings.worker_shared_secret
    if secret is None:
        pytest.skip(
            "WORKER_SHARED_SECRET is not configured in the live .env; "
            "the internal worker API fails closed without it"
        )

    client = WorkerApiClient(base_url=base_url, shared_secret=secret.get_secret_value())
    try:
        claim = client.claim(processor_name="fir_report_text_v1", processor_version="1.0.0")
        assert claim.job is None  # nothing seeded a queued job for this suite to find

        with pytest.raises(InputResolutionUnavailableError):
            client.fetch_input(uuid4(), claim_token="irrelevant-no-real-claim-exists")
    finally:
        client.close()

    pytest.skip(
        "blocked: a full claim -> parse -> SUCCEEDED live run is not attempted here -- "
        "GET /api/v1/internal/worker-jobs/{job_id}/input does not exist yet (see "
        "docs/architecture/structured-processing-worker.md's 'Input-access boundary' "
        "section); this test only proves the real client reaches the real API and that "
        "the documented gap is genuine, and never fabricates a completed-parse pass"
    )
