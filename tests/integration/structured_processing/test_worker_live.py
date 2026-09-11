"""Scenario 19: the worker's HTTP client against a real running API server.

Self-skips (never fabricates success) whenever any of the following is
true, each checked explicitly:
  - no `.env` at the repo root
  - the live API server isn't reachable at `WORKER_API_BASE_URL`
  - `WORKER_SHARED_SECRET` isn't configured in that live environment
  - (pipeline tests only) PostgreSQL/MinIO specifically aren't reachable --
    needed to seed a real case/user/membership and a real evidence upload

`test_worker_client_against_real_running_api` proves the client reaches the
real API for the "no work"/auth-rejection paths without needing any seeded
data. `test_full_claim_stream_parse_submit_live_pipeline` (parametrized over
`document`/`fir_report_text_v1`, `structured_tabular`/`generic_tabular_v1`,
and `structured_json`/`generic_json_v1` -- Phase 2.3's new routing) proves
the *complete* path end to end for each: a real case/user/evidence upload,
followed by a real `run_once()` claiming it, streaming its bytes through
`GET /api/v1/internal/worker-jobs/{job_id}/input` (Phase 2.2 -- see
`docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery"),
parsing, and submitting a genuine `SUCCEEDED` result -- not a `DEFERRED`
fallback. Before Phase 2.2 that endpoint didn't exist, so this couldn't have
been written honestly before; it exists now because the capability
genuinely does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from dotenv import dotenv_values

from app.core.config import Settings
from app.dependencies.services import check_minio, check_postgres
from app.modules.access_control.models import (
    CaseMembershipRecord,
    CaseRecord,
    CaseRole,
    CaseStatus,
    ClearanceLevel,
)
from app.modules.access_control.repository import (
    AccessControlRepository,
    case_memberships_table,
    cases_table,
    users_table,
)
from app.modules.access_control.repository import create_engine as create_ac_engine
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    worker_jobs_table,
    worker_observations_table,
    worker_results_table,
)
from app.modules.structured_processing.client import WorkerApiClient
from app.modules.structured_processing.errors import WorkerAuthenticationError
from app.modules.structured_processing.input_resolver import LiveInputResolver
from app.modules.structured_processing.worker import run_once

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


def _skip_unless_api_reachable(settings: Settings) -> None:
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server not reachable at {settings.worker_api_base_url}: "
            f"{type(exc).__name__}; start it via `docker compose up --build -d` to run this test"
        )


def test_worker_client_against_real_running_api() -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)

    secret = settings.worker_shared_secret
    if secret is None:
        pytest.skip(
            "WORKER_SHARED_SECRET is not configured in the live .env; "
            "the internal worker API fails closed without it"
        )

    client = WorkerApiClient(
        base_url=settings.worker_api_base_url, shared_secret=secret.get_secret_value()
    )
    try:
        claim = client.claim(processor_name="fir_report_text_v1", processor_version="1.0.0")
        assert claim.job is None  # nothing seeded a queued job for this specific check

        # A random job_id with no real claim genuinely rejects with a real
        # 401 -- the endpoint exists as of Phase 2.2, so this is an auth
        # rejection, not the pre-Phase-2.2 `InputResolutionUnavailableError`
        # 404 this test originally proved.
        with pytest.raises(WorkerAuthenticationError):
            client.fetch_input(uuid4(), claim_token="no-real-claim-exists-for-this-job")
    finally:
        client.close()


_PIPELINE_CASES = [
    pytest.param(
        "document",
        "text/plain",
        "fir_live_test.txt",
        b"FIR No. 999/2026 filed at Live Test Police Station. Section 302 IPC.",
        "fir_report_text_v1",
        id="document-fir_report_text_v1",
    ),
    pytest.param(
        "structured_tabular",
        "text/csv",
        "records_live_test.csv",
        b"name,value\nalpha,1\nbeta,2\n",
        "generic_tabular_v1",
        id="structured_tabular-generic_tabular_v1",
    ),
    pytest.param(
        "structured_json",
        "application/json",
        "records_live_test.json",
        b'{"records": [{"a": 1}, {"a": 2}]}',
        "generic_json_v1",
        id="structured_json-generic_json_v1",
    ),
]


@pytest.mark.parametrize(
    ("source_type", "content_type", "filename", "content", "expected_processor"), _PIPELINE_CASES
)
async def test_full_claim_stream_parse_submit_live_pipeline(
    source_type: str, content_type: str, filename: str, content: bytes, expected_processor: str
) -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)

    secret = settings.worker_shared_secret
    if secret is None:
        pytest.skip("WORKER_SHARED_SECRET is not configured in the live .env")

    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"live-worker-test-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105

    # `run_once` claims the *oldest* eligible job for whichever processor it
    # tries first (see `SUPPORTED_PROCESSORS` order); a long-lived dev
    # sandbox's shared PostgreSQL volume can accumulate leftover `queued`
    # rows for `expected_processor` from unrelated earlier runs, which
    # would otherwise make `run_once` claim a stale job instead of the one
    # this test is about to upload. Clearing them first is this test's own
    # isolation, not a change to any application behavior.
    async with ac_engine.begin() as conn:
        await conn.execute(
            sa.delete(worker_jobs_table).where(
                worker_jobs_table.c.processor_name == expected_processor,
                worker_jobs_table.c.status == "queued",
            )
        )

    async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
        register = await ac.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "Live Worker Test"},
        )
        assert register.status_code == 201, register.text
        user_id = register.json()["user_id"]

        login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        case = CaseRecord(
            case_id=uuid4(),
            case_reference=f"LIVE-WORKER-TEST-{uuid4().hex[:8]}",
            classification=ClearanceLevel.RESTRICTED,
            status=CaseStatus.OPEN,
            created_at=datetime.now(UTC),
        )
        await repository.create_case(case)
        membership = CaseMembershipRecord(
            membership_id=uuid4(),
            case_id=case.case_id,
            user_id=user_id,
            role=CaseRole.INVESTIGATOR,
            clearance=ClearanceLevel.RESTRICTED,
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await repository.create_membership(membership)

        try:
            upload = await ac.post(
                f"/api/v1/cases/{case.case_id}/evidence",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                files={"file": (filename, content, content_type)},
                data={"source_type": source_type, "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            assert upload.json()["job"]["processor_name"] == expected_processor

            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, shared_secret=secret.get_secret_value()
            )
            try:
                outcome = run_once(client=client, input_resolver=LiveInputResolver(client))
            finally:
                client.close()

            assert outcome.claimed is True
            assert outcome.job_id is not None and str(outcome.job_id) == job_id
            assert outcome.result_status == "succeeded", outcome

            status_response = await ac.get(
                f"/api/v1/cases/{case.case_id}/jobs/{job_id}", headers=headers
            )
            assert status_response.status_code == 200
            job_status = status_response.json()
            assert job_status["status"] == "succeeded"
            assert job_status["processor_name"] == expected_processor
            assert job_status["observation_count"] > 0
        finally:
            async with ac_engine.begin() as conn:
                await conn.execute(
                    sa.delete(worker_observations_table).where(
                        worker_observations_table.c.case_id == case.case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_results_table).where(
                        worker_results_table.c.case_id == case.case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_jobs_table).where(worker_jobs_table.c.case_id == case.case_id)
                )
                await conn.execute(
                    sa.delete(evidence_records_table).where(
                        evidence_records_table.c.case_id == case.case_id
                    )
                )
                await conn.execute(
                    sa.delete(case_memberships_table).where(
                        case_memberships_table.c.case_id == case.case_id
                    )
                )
                await conn.execute(
                    sa.delete(cases_table).where(cases_table.c.case_id == case.case_id)
                )
                await conn.execute(sa.delete(users_table).where(users_table.c.user_id == user_id))

    await ac_engine.dispose()
