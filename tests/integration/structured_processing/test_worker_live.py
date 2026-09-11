"""Scenario 19: the worker's HTTP client against a real running API server.

Self-skips (never fabricates success) whenever any of the following is
true, each checked explicitly:
  - no `.env` at the repo root
  - the live API server isn't reachable at `WORKER_API_BASE_URL`
  - `WORKER_TOKEN` isn't configured in that live environment
  - (pipeline tests only) PostgreSQL/MinIO specifically aren't reachable --
    needed to seed a real case/user/membership and a real evidence upload

Since Aditya's Phase 2 worker-identity hardening, a configured `WORKER_TOKEN`
alone is not sufficient -- it must also match a real, active
`worker_credentials` row scoped to the processors this test needs. Rather
than requiring a developer to have already run the trusted-operator CLI
before this suite can run at all, `_ensure_worker_credential` provisions
one directly (idempotently, by digest) the first time this file runs
against a given database -- exactly the shape
`uv run python -m app.modules.access_control.worker_credentials create`
would produce, just inlined so a live run needs no separate manual step.

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

import asyncio
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
    WorkerCredentialRecord,
    WorkerCredentialStatus,
)
from app.modules.access_control.repository import (
    AccessControlRepository,
    case_memberships_table,
    cases_table,
    users_table,
)
from app.modules.access_control.repository import create_engine as create_ac_engine
from app.modules.access_control.worker_credentials import (
    hash_worker_credential,
    resolve_worker_pepper,
)
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    worker_jobs_table,
    worker_observations_table,
    worker_results_table,
)
from app.modules.structured_processing.client import WorkerApiClient
from app.modules.structured_processing.errors import WorkerAuthenticationError
from app.modules.structured_processing.input_resolver import LiveInputResolver
from app.modules.structured_processing.worker import SUPPORTED_PROCESSORS, run_once

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    # tests/conftest.py sets a fixed test-only WORKER_CREDENTIAL_PEPPER in
    # os.environ for the rest of the suite's sake. Without forcing this key
    # into kwargs explicitly (even as None), pydantic-settings would fall
    # back to that OS-env value instead of .env's real (here: absent) one
    # -- diverging from what the live API container itself resolves via
    # compose.yaml's own `${WORKER_CREDENTIAL_PEPPER:-}` passthrough, and
    # causing `_ensure_worker_credential`'s digest to mismatch what the
    # live server verifies against.
    kwargs.setdefault("worker_credential_pepper", None)
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def _skip_unless_api_reachable(settings: Settings) -> None:
    """`/healthz` alone only proves the API *process* is alive -- it says nothing about
    whether its own dependencies are reachable. Checking `/readyz` too means a
    dependency outage (e.g. Postgres/Neo4j/Redis/MinIO down while the API process
    itself is still up) self-skips cleanly here, rather than surfacing as a raw
    connection `OSError` deep inside this suite's own first real database call."""
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
        httpx.get(f"{settings.worker_api_base_url}/readyz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server or its dependencies not reachable at "
            f"{settings.worker_api_base_url}: {type(exc).__name__}; start it via "
            f"`docker compose up --build -d` to run this test"
        )


async def _ensure_worker_credential(settings: Settings, token: str) -> None:
    """Idempotently provision a worker credential bound to the exact `token` given,
    scoped to every processor `structured_processing.worker` supports.

    Deliberately does *not* call `worker_credentials.create_worker_credential`
    -- that function always mints its own fresh random token (the right
    behavior for the trusted-operator CLI, which hands the plaintext back to
    an operator) and has no way to bind a credential to an already-known
    token. This helper instead builds the `WorkerCredentialRecord` directly
    with `credential_digest = hash_worker_credential(token, pepper)`, so the
    developer's own configured `WORKER_TOKEN` is what actually authenticates.
    Safe to call repeatedly against the same database: a matching *active*
    digest already existing is a no-op, not an error.

    A digest is unique per row regardless of status, and revocation is
    intentionally permanent (no production "reactivate" path exists --
    that would undermine what revocation is for). So a *revoked* row with
    a matching digest means this literal `WORKER_TOKEN` value can never be
    (re)provisioned again in this database; that's not this helper's call
    to fix silently, since doing so would require weakening real
    access-control semantics. Fail with an actionable message instead of
    letting the INSERT hit the digest's unique constraint and surface a
    confusing `IntegrityError`.
    """
    repository = AccessControlRepository(create_ac_engine(settings))
    try:
        pepper = resolve_worker_pepper(settings)
        digest = hash_worker_credential(token, pepper)
        existing = await repository.get_worker_credential_by_digest(digest)
        if existing is not None:
            if existing.status is WorkerCredentialStatus.ACTIVE:
                return
            pytest.fail(
                f"worker credential for this token literal was revoked "
                f"(worker_id={existing.worker_id}); revocation is permanent -- "
                f"change WORKER_TOKEN in .env, then rerun"
            )
        record = WorkerCredentialRecord(
            worker_id=uuid4(),
            display_name="structured-processing-worker-live-test",
            status=WorkerCredentialStatus.ACTIVE,
            allowed_processor_names=tuple(name for name, _version in SUPPORTED_PROCESSORS),
            credential_digest=digest,
            created_at=datetime.now(UTC),
            rotated_at=None,
            revoked_at=None,
        )
        await repository.create_worker_credential(record)
    finally:
        await repository.close()


def test_worker_client_against_real_running_api() -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)

    secret = settings.worker_token
    if secret is None:
        pytest.skip(
            "WORKER_TOKEN is not configured in the live .env; "
            "the internal worker API fails closed without it"
        )

    asyncio.run(_ensure_worker_credential(settings, secret.get_secret_value()))

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

    secret = settings.worker_token
    if secret is None:
        pytest.skip("WORKER_TOKEN is not configured in the live .env")

    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, secret.get_secret_value())

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
