"""The communication worker's HTTP client against a real running API server.

Mirrors `tests/integration/structured_processing/test_worker_live.py`
exactly (same self-skip conditions, same `_ensure_worker_credential`
pattern for Aditya's Phase 2 worker-identity hardening). Self-skips (never
fabricates success) whenever any of the following is true, each checked
explicitly:
  - no `.env` at the repo root
  - the live API server isn't reachable at `WORKER_API_BASE_URL`
  - `WORKER_TOKEN` isn't configured in that live environment
  - (pipeline test only) PostgreSQL/MinIO specifically aren't reachable --
    needed to seed a real case/user/membership and a real evidence upload

`test_worker_client_against_real_running_api` proves the client reaches the
real API for the "no work"/auth-rejection paths without needing any seeded
data. `test_full_claim_stream_parse_submit_live_pipeline` (parametrized
over `audio`/`audio_metadata_v1` and `chat`/`generic_social_json_v1` -- the
only two source types `evidence_lifecycle/routing.py` currently routes to
this module, see `docs/architecture/communication-processing-worker.md`'s
"Routing boundary" section) proves the *complete* path end to end for
each: a real case/user/evidence upload, followed by a real `run_once()`
claiming it, streaming its bytes through
`GET /api/v1/internal/worker-jobs/{job_id}/input`, building the correct
typed `InputPayload`, parsing, and submitting a genuine `SUCCEEDED` result.

`transcript_import_v1`/`diarization_import_v1`/`whatsapp_export_v1`/
`telegram_export_v1`/`instagram_export_v1` are proven instead at the unit
level (`tests/unit/communication_processing/test_worker_orchestration.py`,
via `StaticInputResolver`) since no live `source_type` routes a real
upload to them today -- this test never fabricates a live pass for a path
that cannot actually occur through the real upload endpoint.
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
from app.modules.communication_processing.client import WorkerApiClient
from app.modules.communication_processing.errors import WorkerAuthenticationError
from app.modules.communication_processing.input_resolver import LiveInputResolver
from app.modules.communication_processing.worker import SUPPORTED_PROCESSORS, run_once
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    worker_jobs_table,
    worker_observations_table,
    worker_results_table,
)
from tests.fixtures.access_control.factories import make_user_record
from tests.fixtures.communication_processing.builders import (
    build_generic_json_export,
    build_wav_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

#: A fixed literal, *not* the configured `WORKER_TOKEN` value: this repo's
#: local dev `.env` carries exactly one shared `WORKER_TOKEN`, and Aditya's
#: Phase 2 worker-identity hardening scopes a credential's
#: `allowed_processor_names` to whichever module provisions it first (see
#: `_ensure_worker_credential`, which is a no-op once a matching-digest
#: credential already exists). Reusing `settings.worker_token` verbatim
#: here -- the same value `structured_processing`'s identical live test
#: also reads -- would make whichever suite runs first "win" the shared
#: credential's scope and 403 the other. A distinct literal gives this
#: worker its own credential row, entirely decoupled from
#: `structured_processing`'s, matching how two independent real worker
#: deployments would each hold their own distinct token. `settings.
#: worker_token` is still used as the "is live testing configured at all"
#: skip-gate below -- only the actual authentication value differs.
_COMMUNICATION_WORKER_TOKEN = "dev-only-communication-worker-token-change-me-v2"  # noqa: S105

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
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
    scoped to every processor `communication_processing.worker` supports.

    See `tests/integration/structured_processing/test_worker_live.py`'s
    identical helper for the full reasoning -- this is the same pattern.

    A digest is unique per row regardless of status, and revocation is
    intentionally permanent (no production "reactivate" path exists --
    that would undermine what revocation is for). So a *revoked* row with
    a matching digest means this literal token value can never be
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
                f"change the token literal this test provisions with, "
                f"then rerun"
            )
        record = WorkerCredentialRecord(
            worker_id=uuid4(),
            display_name="communication-processing-worker-live-test",
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

    token = settings.worker_token
    if token is None:
        pytest.skip(
            "WORKER_TOKEN is not configured in the live .env; "
            "the internal worker API fails closed without it"
        )

    asyncio.run(_ensure_worker_credential(settings, _COMMUNICATION_WORKER_TOKEN))

    client = WorkerApiClient(
        base_url=settings.worker_api_base_url, worker_token=_COMMUNICATION_WORKER_TOKEN
    )
    try:
        claim = client.claim(processor_name="audio_metadata_v1", processor_version="1.0.0")
        assert claim.job is None  # nothing seeded a queued job for this specific check

        with pytest.raises(WorkerAuthenticationError):
            client.fetch_input(uuid4(), claim_token="no-real-claim-exists-for-this-job")
    finally:
        client.close()


_PIPELINE_CASES = [
    pytest.param(
        "audio",
        "audio/wav",
        "call_live_test.wav",
        build_wav_bytes(duration_seconds=0.5),
        "audio_metadata_v1",
        id="audio-audio_metadata_v1",
    ),
    pytest.param(
        "chat",
        "application/json",
        "export_live_test.json",
        build_generic_json_export(records=[{"sender": "alice", "text": "hello from live test"}]),
        "generic_social_json_v1",
        id="chat-generic_social_json_v1",
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

    token = settings.worker_token
    if token is None:
        pytest.skip("WORKER_TOKEN is not configured in the live .env")

    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, _COMMUNICATION_WORKER_TOKEN)

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"comm-live-worker-test-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105

    # Clear any leftover `queued` rows for `expected_processor` from earlier
    # runs in this long-lived shared sandbox -- see the structured-processing
    # sibling test for the full reasoning. This test's own isolation, not a
    # change to any application behavior.
    async with ac_engine.begin() as conn:
        await conn.execute(
            sa.delete(worker_jobs_table).where(
                worker_jobs_table.c.processor_name == expected_processor,
                worker_jobs_table.c.status == "queued",
            )
        )

    # No public self-registration exists (G5) -- seed the user directly.
    seeded_user = make_user_record(email_normalized=email)
    await repository.create_user(seeded_user)
    user_id = str(seeded_user.user_id)

    async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
        login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        case = CaseRecord(
            case_id=uuid4(),
            case_reference=f"COMM-LIVE-WORKER-TEST-{uuid4().hex[:8]}",
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
                base_url=settings.worker_api_base_url, worker_token=_COMMUNICATION_WORKER_TOKEN
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
