"""Scenario 19: the full worker-identity lifecycle against a real running API server.

provision worker -> upload evidence -> permitted worker claims -> secure
stream -> submit result -> denied second-worker attempt -> revocation
denies future access.

Self-skips (never fabricates a pass), mirroring
`tests/integration/structured_processing/test_worker_live.py`'s pattern
exactly, whenever:
  - no `.env` at the repo root
  - the live API server isn't reachable at `WORKER_API_BASE_URL`
  - PostgreSQL/MinIO specifically aren't reachable through it

Uses only synthetic evidence content (a tiny CSV, routed to
`structured_tabular`/`generic_tabular_v1`) and provisions two throwaway
worker credentials directly via `app.modules.access_control.worker_credentials`
(the exact same functions the trusted-operator CLI calls) -- everything
created is cleaned up in a `finally` block, including the worker credential
rows themselves.
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
    worker_credentials_table,
)
from app.modules.access_control.repository import create_engine as create_ac_engine
from app.modules.access_control.worker_credentials import (
    create_worker_credential,
    resolve_worker_pepper,
    revoke_worker_credential,
)
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    worker_jobs_table,
    worker_observations_table,
    worker_results_table,
)
from tests.fixtures.access_control.factories import make_user_record

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)

_CSV_CONTENT = b"name,value\nalpha,1\nbeta,2\n"


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    # tests/conftest.py sets a fixed test-only WORKER_CREDENTIAL_PEPPER in
    # os.environ for the rest of the suite's sake. Without forcing this key
    # into kwargs explicitly (even as None), pydantic-settings would fall
    # back to that OS-env value instead of .env's real (here: absent) one
    # -- diverging from what the live API container itself resolves via
    # compose.yaml's own `${WORKER_CREDENTIAL_PEPPER:-}` passthrough, and
    # causing every credential digest this test computes to mismatch what
    # the live server verifies against.
    kwargs.setdefault("worker_credential_pepper", None)
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def _skip_unless_api_reachable(settings: Settings) -> None:
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server not reachable at {settings.worker_api_base_url}: "
            f"{type(exc).__name__}; start it via `docker compose up --build -d` to run this test"
        )


def _succeeded_result_payload(*, job_id: str, case_id: str, evidence_id: str) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "job_id": job_id,
        "case_id": case_id,
        "evidence_id": evidence_id,
        "status": "succeeded",
        "observations": [],
        "derived_artifacts": [],
        "checkpoint": None,
        "error": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }


async def test_full_worker_identity_lifecycle_against_live_stack() -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    pepper = resolve_worker_pepper(settings)
    ac_engine = create_ac_engine(settings)
    ac_repository = AccessControlRepository(ac_engine)

    worker_a, worker_a_token = await create_worker_credential(
        ac_repository,
        display_name="live-lifecycle-worker-a",
        allowed_processor_names=["generic_tabular_v1"],
        pepper=pepper,
        now=datetime.now(UTC),
    )
    worker_b, worker_b_token = await create_worker_credential(
        ac_repository,
        display_name="live-lifecycle-worker-b",
        allowed_processor_names=["generic_tabular_v1"],
        pepper=pepper,
        now=datetime.now(UTC),
    )

    email = f"live-identity-test-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105
    case_id = None
    user_id = None
    try:
        # No public self-registration exists (G5) -- seed the user directly
        # against the same live Postgres `ac_repository` already writes
        # case/membership rows to, then log in through the real HTTP flow.
        seeded_user = make_user_record(email_normalized=email)
        await ac_repository.create_user(seeded_user)
        user_id = str(seeded_user.user_id)
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"LIVE-IDENTITY-TEST-{uuid4().hex[:8]}",
                classification=ClearanceLevel.RESTRICTED,
                status=CaseStatus.OPEN,
                created_at=datetime.now(UTC),
            )
            await ac_repository.create_case(case)
            case_id = case.case_id
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
            await ac_repository.create_membership(membership)

            upload = await ac.post(
                f"/api/v1/cases/{case.case_id}/evidence",
                headers={**user_headers, "Idempotency-Key": str(uuid4())},
                files={"file": ("records.csv", _CSV_CONTENT, "text/csv")},
                data={"source_type": "structured_tabular", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            assert upload.json()["job"]["processor_name"] == "generic_tabular_v1"

            # --- permitted worker claims ---
            claim = await ac.post(
                "/api/v1/internal/worker-jobs/claim",
                headers={"Authorization": f"Bearer {worker_a_token}"},
                json={"processor_name": "generic_tabular_v1", "processor_version": "1.0.0"},
            )
            assert claim.status_code == 200, claim.text
            claim_body = claim.json()
            assert claim_body["job"]["job_id"] == job_id
            claim_token = claim_body["claim_token"]
            assert claim_token

            # --- secure stream: the claiming worker reads its own input ---
            stream = await ac.get(
                f"/api/v1/internal/worker-jobs/{job_id}/input",
                headers={"Authorization": f"Bearer {worker_a_token}", "X-Claim-Token": claim_token},
            )
            assert stream.status_code == 200, stream.text
            assert stream.content == _CSV_CONTENT

            # --- a second, otherwise-valid, worker cannot use worker A's claim token ---
            denied_stream = await ac.get(
                f"/api/v1/internal/worker-jobs/{job_id}/input",
                headers={"Authorization": f"Bearer {worker_b_token}", "X-Claim-Token": claim_token},
            )
            assert denied_stream.status_code == 401

            # --- submit result as the rightful claimant ---
            submit = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/result",
                headers={"Authorization": f"Bearer {worker_a_token}", "X-Claim-Token": claim_token},
                json=_succeeded_result_payload(
                    job_id=job_id,
                    case_id=str(case.case_id),
                    evidence_id=upload.json()["evidence"]["evidence_id"],
                ),
            )
            assert submit.status_code == 200, submit.text

            # --- denied second-worker attempt: worker B cannot submit A's result ---
            denied_submit = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/result",
                headers={"Authorization": f"Bearer {worker_b_token}", "X-Claim-Token": claim_token},
                json=_succeeded_result_payload(
                    job_id=job_id,
                    case_id=str(case.case_id),
                    evidence_id=upload.json()["evidence"]["evidence_id"],
                ),
            )
            assert denied_submit.status_code == 401

            # --- revocation denies future access ---
            await revoke_worker_credential(
                ac_repository, worker_id=worker_a.worker_id, now=datetime.now(UTC)
            )
            future_claim = await ac.post(
                "/api/v1/internal/worker-jobs/claim",
                headers={"Authorization": f"Bearer {worker_a_token}"},
                json={"processor_name": "generic_tabular_v1", "processor_version": "1.0.0"},
            )
            assert future_claim.status_code == 401

            # A previously-revoked worker also can no longer stream/submit,
            # even for a job it legitimately claimed before revocation.
            future_stream = await ac.get(
                f"/api/v1/internal/worker-jobs/{job_id}/input",
                headers={"Authorization": f"Bearer {worker_a_token}", "X-Claim-Token": claim_token},
            )
            assert future_stream.status_code == 401
    finally:
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_credentials_table).where(
                    worker_credentials_table.c.worker_id.in_(
                        [worker_a.worker_id, worker_b.worker_id]
                    )
                )
            )
            if case_id is not None:
                await conn.execute(
                    sa.delete(worker_observations_table).where(
                        worker_observations_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_results_table).where(worker_results_table.c.case_id == case_id)
                )
                await conn.execute(
                    sa.delete(worker_jobs_table).where(worker_jobs_table.c.case_id == case_id)
                )
                await conn.execute(
                    sa.delete(evidence_records_table).where(
                        evidence_records_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(case_memberships_table).where(
                        case_memberships_table.c.case_id == case_id
                    )
                )
                await conn.execute(sa.delete(cases_table).where(cases_table.c.case_id == case_id))
            if user_id is not None:
                await conn.execute(sa.delete(users_table).where(users_table.c.user_id == user_id))
        await ac_engine.dispose()
