"""The media worker's HTTP client against a real running API server, through to Neo4j.

Mirrors `tests/integration/communication_processing/test_communication_worker_live.py`'s
self-skip pattern and `_ensure_worker_credential` helper exactly, extended
with this phase's own required proof: that a real media observation flows
through the *existing* durable graph-projection queue into a real,
case-scoped Neo4j projection, confirmed both by a direct database read and
by the real HTTP graph-read endpoint -- and that re-running the projector
never duplicates the projected node.

Self-skips (never fabricates a pass) whenever any of the following is true:
  - no `.env` at the repo root
  - the live API server isn't reachable at `WORKER_API_BASE_URL`
  - `WORKER_TOKEN` isn't configured in that live environment
  - (pipeline test only) PostgreSQL/Neo4j/MinIO specifically aren't
    reachable -- needed to seed a real case/user/membership, a real
    evidence upload, and a real graph projection
  - (video case only) `ffmpeg`/`ffprobe` are not on `PATH` -- needed to
    build a real synthetic MP4 fixture and to probe it

Only `media_metadata_v1` is exercised here: it is the only processor
`evidence_lifecycle/routing.py` ever routes a real image/video upload to
(see `worker.SUPPORTED_PROCESSORS`'s docstring for why `media_detection_v1`
is proven at the unit level only, via `StaticInputResolver`, not here).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from dotenv import dotenv_values

from app.core.config import Settings
from app.dependencies.services import check_minio, check_neo4j, check_postgres
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
    graph_projection_jobs_table,
    worker_jobs_table,
    worker_observations_table,
    worker_results_table,
)
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_pg_engine_for_graph
from app.modules.graph.projector import run_batch
from app.modules.graph.queries import list_case_observations
from app.modules.graph.repository import Neo4jGraphRepository, create_driver
from app.modules.media_processing.client import WorkerApiClient
from app.modules.media_processing.errors import WorkerAuthenticationError
from app.modules.media_processing.input_resolver import LiveInputResolver
from app.modules.media_processing.worker import SUPPORTED_PROCESSORS, run_once
from tests.fixtures.media_processing.synthetic import (
    ffmpeg_available,
    make_png_bytes,
    make_synthetic_mp4_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

#: A fixed literal, distinct from every other module's live-test worker
#: token -- see `test_communication_worker_live.py`'s identical constant
#: for the full reasoning (this repo's shared dev credential table would
#: otherwise let whichever suite runs first "win" the scope).
_MEDIA_WORKER_TOKEN = "dev-only-media-worker-token-change-me-v1"  # noqa: S105

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
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server not reachable at {settings.worker_api_base_url}: "
            f"{type(exc).__name__}; start it via `docker compose up --build -d` to run this test"
        )


async def _ensure_worker_credential(settings: Settings, token: str) -> None:
    """Idempotently provision a worker credential bound to `token`, scoped to
    every processor `media_processing.worker` claims live. Mirrors
    `test_communication_worker_live.py`'s identical helper."""
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
                f"change the token literal this test provisions with, then rerun"
            )
        record = WorkerCredentialRecord(
            worker_id=uuid4(),
            display_name="media-processing-worker-live-test",
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

    asyncio.run(_ensure_worker_credential(settings, _MEDIA_WORKER_TOKEN))

    client = WorkerApiClient(
        base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
    )
    try:
        claim = client.claim(processor_name="media_metadata_v1", processor_version="1.0.0")
        assert claim.job is None  # nothing seeded a queued job for this specific check

        with pytest.raises(WorkerAuthenticationError):
            client.fetch_input(uuid4(), claim_token="no-real-claim-exists-for-this-job")
    finally:
        client.close()


def _image_case() -> Any:
    return pytest.param(
        "image", "image/png", "photo_live_test.png", make_png_bytes(), id="image-media_metadata_v1"
    )


def _video_case() -> Any:
    if not ffmpeg_available():
        return pytest.param(
            "video",
            "video/mp4",
            "clip_live_test.mp4",
            b"",
            id="video-media_metadata_v1",
            marks=pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH; cannot build synthetic mp4"),
        )
    return pytest.param(
        "video",
        "video/mp4",
        "clip_live_test.mp4",
        make_synthetic_mp4_bytes(duration_seconds=1.0),
        id="video-media_metadata_v1",
    )


@pytest.mark.parametrize(
    ("source_type", "content_type", "filename", "content"), [_image_case(), _video_case()]
)
async def test_full_upload_claim_stream_verify_process_submit_project_live_pipeline(
    source_type: str, content_type: str, filename: str, content: bytes
) -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)

    token = settings.worker_token
    if token is None:
        pytest.skip("WORKER_TOKEN is not configured in the live .env")

    try:
        await check_postgres(settings)
        await check_minio(settings)
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/Neo4j/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, _MEDIA_WORKER_TOKEN)

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"media-live-worker-test-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105

    graph_driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(graph_driver)
    case_id = None
    user_id = None
    try:
        # Clear any leftover `queued` rows from earlier runs in this
        # long-lived shared sandbox -- this test's own isolation, not a
        # change to any application behavior.
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_jobs_table).where(
                    worker_jobs_table.c.processor_name == "media_metadata_v1",
                    worker_jobs_table.c.status == "queued",
                )
            )

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            register = await ac.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": password,
                    "display_name": "Media Live Worker Test",
                },
            )
            assert register.status_code == 201, register.text
            user_id = register.json()["user_id"]

            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"MEDIA-LIVE-WORKER-TEST-{uuid4().hex[:8]}",
                classification=ClearanceLevel.RESTRICTED,
                status=CaseStatus.OPEN,
                created_at=datetime.now(UTC),
            )
            await repository.create_case(case)
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
            await repository.create_membership(membership)

            upload = await ac.post(
                f"/api/v1/cases/{case.case_id}/evidence",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                files={"file": (filename, content, content_type)},
                data={"source_type": source_type, "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            assert upload.json()["job"]["processor_name"] == "media_metadata_v1"

            # --- real worker: claim -> stream -> SHA-256 verify -> decode/probe -> submit ---
            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
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
            assert job_status["processor_name"] == "media_metadata_v1"
            assert job_status["observation_count"] > 0

            # No raw media bytes, object storage URI, or credential ever
            # appears in this status response.
            status_text = status_response.text
            assert content_type != "" and content[:8] not in status_text.encode()
            assert "object_uri" not in status_text
            assert settings.worker_token.get_secret_value() not in status_text  # type: ignore[union-attr]

        # --- durable graph-projection job was enqueued exactly once ---
        async with ac_engine.begin() as conn:
            obs_row = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.job_id == job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            assert obs_row is not None, "no worker_observations row was persisted"
            observation_id = obs_row["observation_id"]
            assert obs_row["observation_type"] == "media_metadata"

            proj_rows = (
                (
                    await conn.execute(
                        sa.select(graph_projection_jobs_table).where(
                            graph_projection_jobs_table.c.observation_id == observation_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert len(proj_rows) == 1, "expected exactly one graph_projection_jobs row"
        assert proj_rows[0]["status"] == "queued"

        # --- run the graph projector (--once equivalent, in-process) TWICE ---
        pg_engine = create_pg_engine_for_graph(settings)
        outbox = GraphProjectionOutboxRepository(pg_engine)
        try:
            first_summary = await run_batch(
                outbox,
                graph_repository,
                now=datetime.now(UTC),
                lease_seconds=settings.graph_projection_lease_seconds,
                batch_size=settings.graph_projection_batch_size,
            )
            second_summary = await run_batch(
                outbox,
                graph_repository,
                now=datetime.now(UTC),
                lease_seconds=settings.graph_projection_lease_seconds,
                batch_size=settings.graph_projection_batch_size,
            )
        finally:
            await outbox.close()

        own_first_attempts = [
            a for a in first_summary.attempts if a.job.observation_id == observation_id
        ]
        assert len(own_first_attempts) == 1, "this test's own projection job was not attempted"
        assert own_first_attempts[0].outcome == "succeeded"
        # The job is already terminal by the second run -- it is never
        # re-claimed, so no second Neo4j write (and no duplicate node) is
        # even attempted.
        own_second_attempts = [
            a for a in second_summary.attempts if a.job.observation_id == observation_id
        ]
        assert own_second_attempts == []

        # --- real, case-scoped Neo4j query confirms exactly one projection ---
        page = await list_case_observations(graph_repository, case_id)
        matching = [
            item for item in page.items if item.observation.observation_id == observation_id
        ]
        assert len(matching) == 1, "projected media observation not found, or duplicated"
        assert str(matching[0].observation.evidence_id) is not None

        # --- and via the real case-scoped read API itself ---
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            graph_response = await ac.get(
                f"/api/v1/cases/{case_id}/graph/observations", headers=headers
            )
        assert graph_response.status_code == 200, graph_response.text
        api_body = graph_response.json()
        api_items = api_body["items"]
        api_matching = [i for i in api_items if i["observation_id"] == str(observation_id)]
        assert len(api_matching) == 1, "not exactly one projected observation via the read API"

        # The read API response never leaks an object URI, raw media
        # bytes, or a credential -- only safe, allow-listed graph fields.
        response_text = graph_response.text
        assert "object_uri" not in response_text
        assert settings.worker_token.get_secret_value() not in response_text  # type: ignore[union-attr]

        # --- idempotent resubmission: the server, not just this client, must
        # treat an exact-payload replay as a no-op, never a duplicate write ---
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            replay = await ac.get(f"/api/v1/cases/{case_id}/jobs/{job_id}", headers=headers)
        assert replay.status_code == 200
        assert replay.json()["observation_count"] == job_status["observation_count"]
    finally:
        if case_id is not None:
            with contextlib.suppress(Exception):  # best-effort test cleanup
                await graph_repository.write(
                    "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_id)}
                )
        await graph_repository.close()
        async with ac_engine.begin() as conn:
            if case_id is not None:
                await conn.execute(
                    sa.delete(graph_projection_jobs_table).where(
                        graph_projection_jobs_table.c.case_id == case_id
                    )
                )
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
