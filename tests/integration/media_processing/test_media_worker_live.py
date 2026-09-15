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

`evidence_lifecycle/routing.py` routes every real image/video upload to
`media_detection_v1` (Phase 2 closeout -- see
`docs/architecture/phase-2-decisions.md`'s "Real local media inference
closeout"), so that is what this suite claims and asserts on throughout.
The real detector/OCR components are built exactly as `worker.main` would
(`_build_analysis_components`) -- when a detector model asset has been
bootstrapped locally (`bootstrap_models.py`) *and* the live API happens to
route it to this test process, real detections/OCR observations are
produced on top of the always-present metadata observation; when it
hasn't, this degrades to the same metadata-only behavior the pre-closeout
suite always proved, never a fabricated pass either way. This suite never
downloads the model itself.
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
from app.modules.media_processing.worker import (
    SUPPORTED_PROCESSORS,
    _build_analysis_components,
    run_once,
)
from tests.fixtures.media_processing.synthetic import (
    ffmpeg_available,
    find_test_font,
    make_png_bytes,
    make_synthetic_mp4_bytes,
    make_text_png_bytes,
    make_text_video_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

#: A fixed literal, distinct from every other module's live-test worker
#: token -- see `test_communication_worker_live.py`'s identical constant
#: for the full reasoning (this repo's shared dev credential table would
#: otherwise let whichever suite runs first "win" the scope).
_MEDIA_WORKER_TOKEN = "dev-only-media-worker-token-change-me-v2"  # noqa: S105

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    kwargs.setdefault("worker_credential_pepper", None)
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def _real_ocr_fixture() -> tuple[bytes, object]:
    """Build a labelled raster and the real local adapter, or precisely skip.

    Fixture OCR belongs in deterministic unit tests. These live tests prove
    the system Tesseract path and must never substitute synthetic text when
    that local runtime is absent.
    """
    from app.modules.media_processing.errors import OcrRuntimeError
    from app.modules.media_processing.ocr_adapter import ImageOcrAdapter

    font_path = find_test_font()
    if font_path is None:
        pytest.skip("real local OCR unavailable: no TrueType font for labelled fixture")
    try:
        adapter = ImageOcrAdapter()
    except OcrRuntimeError as exc:
        pytest.skip(f"real local OCR unavailable: {exc}")
    return (
        make_text_png_bytes(
            "TRACEX OCR",
            width=640,
            height=180,
            font_path=font_path,
            font_size=72,
            text_x=30,
            text_y=40,
        ),
        adapter,
    )


def _real_ocr_video_fixture() -> tuple[bytes, object]:
    """Build a labelled synthetic MP4 and the real local adapter, or precisely skip.

    Mirrors `_real_ocr_fixture()` for the video-frame path: this live test
    proves the real Tesseract-on-a-real-sampled-video-frame path and must
    never substitute `FixtureOcrAdapter` or a textless clip for a
    genuinely-missing local `ffmpeg`/font/Tesseract runtime.
    """
    from app.modules.media_processing.errors import OcrRuntimeError
    from app.modules.media_processing.ocr_adapter import ImageOcrAdapter

    if not ffmpeg_available():
        pytest.skip("real local OCR unavailable: ffmpeg/ffprobe not on PATH")
    font_path = find_test_font()
    if font_path is None:
        pytest.skip("real local OCR unavailable: no TrueType font for labelled fixture")
    try:
        adapter = ImageOcrAdapter()
    except OcrRuntimeError as exc:
        pytest.skip(f"real local OCR unavailable: {exc}")
    return (
        make_text_video_bytes(
            "TRACEX OCR",
            width=640,
            height=180,
            font_path=font_path,
            font_size=72,
            text_x=30,
            text_y=40,
            duration_seconds=2.0,
            fps=5.0,
        ),
        adapter,
    )


def _skip_unless_api_reachable(settings: Settings) -> None:
    """`/healthz` alone only proves the API *process* is alive -- it says nothing about
    whether its own dependencies are reachable (e.g. this API process running fine on
    the host while the Postgres/Neo4j/Redis/MinIO containers it depends on have gone
    down separately). Checking `/readyz` too means a dependency outage self-skips
    cleanly here, rather than surfacing as a raw, unhelpful connection `OSError` deep
    inside this suite's own first real database call."""
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
        claim = client.claim(processor_name="media_detection_v1", processor_version="1.0.0")
        assert claim.job is None  # nothing seeded a queued job for this specific check

        with pytest.raises(WorkerAuthenticationError):
            client.fetch_input(uuid4(), claim_token="no-real-claim-exists-for-this-job")
    finally:
        client.close()


def _image_case() -> Any:
    return pytest.param(
        "image", "image/png", "photo_live_test.png", make_png_bytes(), id="image-media_detection_v1"
    )


def _video_case() -> Any:
    if not ffmpeg_available():
        return pytest.param(
            "video",
            "video/mp4",
            "clip_live_test.mp4",
            b"",
            id="video-media_detection_v1",
            marks=pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH; cannot build synthetic mp4"),
        )
    return pytest.param(
        "video",
        "video/mp4",
        "clip_live_test.mp4",
        make_synthetic_mp4_bytes(duration_seconds=1.0),
        id="video-media_detection_v1",
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
                    worker_jobs_table.c.processor_name == "media_detection_v1",
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
            assert upload.json()["job"]["processor_name"] == "media_detection_v1"

            # --- real worker: claim -> stream -> SHA-256 verify -> decode/probe -> submit ---
            # Real analysis components, built exactly as `worker.main` would --
            # degrades to metadata-only if no detector model asset has been
            # bootstrapped locally (see the module docstring); never fabricates
            # detections/OCR either way.
            components = _build_analysis_components(settings)
            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
            )
            try:
                outcome = run_once(
                    client=client,
                    input_resolver=LiveInputResolver(client),
                    detector=components.detector,
                    tracker=components.tracker,
                    ocr_adapter=components.ocr_adapter,
                )
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
            assert job_status["processor_name"] == "media_detection_v1"
            assert job_status["observation_count"] > 0

            # No raw media bytes, object storage URI, or credential ever
            # appears in this status response.
            status_text = status_response.text
            assert content_type != "" and content[:8] not in status_text.encode()
            assert "object_uri" not in status_text
            assert settings.worker_token.get_secret_value() not in status_text  # type: ignore[union-attr]

        # --- durable graph-projection job was enqueued exactly once (per observation) ---
        # `media_detection_v1` always emits the metadata observation first,
        # regardless of whether a detector was configured, plus real
        # detection/OCR/tracking observations on top of it when one was --
        # this test follows the metadata observation specifically through
        # projection (proven independent of whether analysis components were
        # available in this run), not "whichever row sorts first."
        async with ac_engine.begin() as conn:
            obs_rows = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.job_id == job_id
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert obs_rows, "no worker_observations row was persisted"
            metadata_rows = [r for r in obs_rows if r["observation_type"] == "media_metadata"]
            assert len(metadata_rows) == 1, "expected exactly one media_metadata observation"
            observation_id = metadata_rows[0]["observation_id"]

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


# ---------------------------------------------------------------------------
# Phase 3: OCR micro-batch live tests (scenarios 24-30)
# ---------------------------------------------------------------------------


async def test_ocr_batch_submission_end_to_end_image_live() -> None:
    """Scenario 24-26: real local OCR → batch submit
    → terminal result → graph outbox exactly once → projector idempotent.

    Self-skips when the live API or PostgreSQL/Neo4j/MinIO are not reachable.
    """
    from app.modules.evidence_lifecycle.repository import (
        graph_projection_jobs_table,
        observation_batches_table,
        observation_transformations_table,
        worker_observations_table,
        worker_results_table,
    )
    from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
    from app.modules.graph.outbox_repository import create_engine as create_pg_engine_for_graph
    from app.modules.graph.projector import run_batch as run_projection_batch
    from app.modules.graph.queries import list_case_observations
    from app.modules.graph.repository import Neo4jGraphRepository, create_driver
    from app.modules.media_processing.image.decoder import decode_image
    from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
    from app.modules.media_processing.ocr_batching import (
        OcrBatchConfig,
        build_terminal_result,
        iter_image_ocr_batches,
    )

    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    png_bytes, adapter = _real_ocr_fixture()

    try:
        await check_postgres(settings)
        await check_minio(settings)
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/Neo4j/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, _MEDIA_WORKER_TOKEN)

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"ocr-batch-live-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105

    graph_driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(graph_driver)
    case_id = None
    user_id = None
    try:
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_jobs_table).where(
                    worker_jobs_table.c.processor_name == "media_detection_v1",
                    worker_jobs_table.c.status == "queued",
                )
            )

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            register = await ac.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": password,
                    "display_name": "OCR Batch Live Test",
                },
            )
            assert register.status_code == 201, register.text
            user_id = register.json()["user_id"]

            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"OCR-BATCH-LIVE-{uuid4().hex[:8]}",
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
                files={"file": ("ocr_test.png", png_bytes, "image/png")},
                data={"source_type": "image", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            assert upload.json()["job"]["processor_name"] == "media_detection_v1"

            # --- Phase 3: claim, real local OCR, batch submit ---
            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
            )
            try:
                claim = client.claim(processor_name="media_detection_v1", processor_version="1.0.0")
                assert claim.job is not None and str(claim.job.job_id) == job_id
                claim_token = claim.claim_token
                assert claim_token

                job = claim.job

                # Resolve evidence bytes via the claim-token-bound endpoint.
                from app.modules.media_processing.input_resolver import LiveInputResolver  # noqa

                resolved = LiveInputResolver(client).resolve(job, claim_token=claim_token)

                image, metadata = decode_image(resolved.data, limits=DEFAULT_MEDIA_LIMITS)
                ocr_results = adapter.run(image, metadata)  # type: ignore[attr-defined]
                assert any("TRACEX" in result.text.upper() for result in ocr_results)

                # Scenario 25: batch submission round-trip.
                batch_config = OcrBatchConfig(batch_size=50, units_total=len(ocr_results))
                obs_ids_submitted: list[str] = []
                for submission in iter_image_ocr_batches(
                    ocr_results,
                    job=job,
                    idempotency_key_prefix=f"{job_id}-ocr-live",
                    batch_config=batch_config,
                    completed_at=datetime.now(UTC),
                ):
                    receipt = client.submit_batch(
                        job_id=job.job_id,
                        claim_token=claim_token,
                        submission=submission,
                    )
                    assert receipt.status in ("accepted", "replayed")
                    obs_ids_submitted.extend(str(o.observation_id) for o in submission.observations)

                # Submit terminal result with observations=[].
                terminal = build_terminal_result(
                    job_id=job.job_id,
                    case_id=job.case_id,
                    evidence_id=job.evidence_id,
                    completed_at=datetime.now(UTC),
                )
                ack = client.submit_result(
                    job_id=job.job_id, claim_token=claim_token, result=terminal
                )
                assert ack.status == "succeeded"

            finally:
                client.close()

            # --- Verify job status ---
            status_response = await ac.get(
                f"/api/v1/cases/{case.case_id}/jobs/{job_id}", headers=headers
            )
            assert status_response.status_code == 200
            job_status = status_response.json()
            assert job_status["status"] == "succeeded"
            assert job_status["processor_name"] == "media_detection_v1"

            # Scenario 27: security invariants -- no raw bytes, URI, or token in response.
            status_text = status_response.text
            assert "object_uri" not in status_text
            if settings.worker_token:
                assert settings.worker_token.get_secret_value() not in status_text

            # --- Verify durable rows ---
            async with ac_engine.begin() as conn:
                obs_rows = (
                    (
                        await conn.execute(
                            sa.select(worker_observations_table).where(
                                worker_observations_table.c.job_id == uuid4().__class__(job_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                # Observations exist for each batch-delivered OCR result.
                batch_obs = [r for r in obs_rows if r["observation_batch_id"] is not None]
                assert len(batch_obs) == len(obs_ids_submitted), (
                    f"expected {len(obs_ids_submitted)} batch-delivered observations, "
                    f"got {len(batch_obs)}"
                )
                # Terminal result observations are empty.
                result_obs = [r for r in obs_rows if r["result_id"] is not None]
                assert result_obs == [], "terminal result must carry no observations"

        # --- Scenario 26: graph outbox exactly once, projector idempotent ---
        if obs_ids_submitted:
            async with ac_engine.begin() as conn:
                outbox_rows = (
                    (
                        await conn.execute(
                            sa.select(graph_projection_jobs_table).where(
                                graph_projection_jobs_table.c.observation_id.in_(
                                    [uuid4().__class__(oid) for oid in obs_ids_submitted]
                                )
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(outbox_rows) == len(obs_ids_submitted), (
                "expected one graph-projection job per submitted observation"
            )
            assert {r["status"] for r in outbox_rows} == {"queued"}

            pg_engine = create_pg_engine_for_graph(settings)
            outbox = GraphProjectionOutboxRepository(pg_engine)
            try:
                # First run projects everything.
                first_summary = await run_projection_batch(
                    outbox,
                    graph_repository,
                    now=datetime.now(UTC),
                    lease_seconds=settings.graph_projection_lease_seconds,
                    batch_size=settings.graph_projection_batch_size,
                )
                # Scenario 28: second run is a no-op (idempotent).
                second_summary = await run_projection_batch(
                    outbox,
                    graph_repository,
                    now=datetime.now(UTC),
                    lease_seconds=settings.graph_projection_lease_seconds,
                    batch_size=settings.graph_projection_batch_size,
                )
            finally:
                await outbox.close()

            obs_id_set = {uuid4().__class__(oid) for oid in obs_ids_submitted}
            first_own = [a for a in first_summary.attempts if a.job.observation_id in obs_id_set]
            assert all(a.outcome == "succeeded" for a in first_own)
            second_own = [a for a in second_summary.attempts if a.job.observation_id in obs_id_set]
            assert second_own == [], "second projector run must produce no new work for these obs"

            # After projection: observations appear in Neo4j.
            post_page = await list_case_observations(graph_repository, case_id)
            projected_ids = {item.observation.observation_id for item in post_page.items}
            for oid in obs_ids_submitted:
                assert uuid4().__class__(oid) in projected_ids, (
                    f"observation {oid} not found in Neo4j after projection"
                )

    finally:
        if case_id is not None:
            with contextlib.suppress(Exception):
                await graph_repository.write(
                    "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_id)}
                )
        await graph_repository.close()
        async with ac_engine.begin() as conn:
            if case_id is not None:
                _case_id_uuid = uuid4().__class__(str(case_id))
                for table in [
                    graph_projection_jobs_table,
                    worker_observations_table,
                    observation_batches_table,
                    observation_transformations_table,
                    worker_results_table,
                    worker_jobs_table,
                    evidence_records_table,
                    case_memberships_table,
                ]:
                    with contextlib.suppress(Exception):
                        await conn.execute(sa.delete(table).where(table.c.case_id == _case_id_uuid))
                await conn.execute(
                    sa.delete(cases_table).where(cases_table.c.case_id == _case_id_uuid)
                )
            if user_id is not None:
                await conn.execute(sa.delete(users_table).where(users_table.c.user_id == user_id))
        await ac_engine.dispose()


async def test_video_frame_ocr_batch_submission_end_to_end_live() -> None:
    """Scenario 26: real local OCR on a real sampled video frame -> batch submit
    -> empty terminal result -> graph outbox exactly once -> projector idempotent.

    Self-skips when the live API, PostgreSQL/Neo4j/MinIO, `ffmpeg`/`ffprobe`,
    or a real local Tesseract/font are not reachable -- never substitutes
    `FixtureOcrAdapter` or a textless video for a genuinely-missing runtime
    (see `_real_ocr_video_fixture`). Uses `FakeObjectDetector` only to enter
    the existing, unchanged video-analysis sampling path deterministically --
    this test proves the OCR/batch/outbox behavior for a video frame, not
    object-detection accuracy (already covered by
    `test_full_upload_claim_stream_verify_process_submit_project_live_pipeline`).
    """
    from app.modules.evidence_lifecycle.repository import (
        media_checkpoints_table,
        media_chunk_manifests_table,
        media_chunk_observations_table,
        media_chunks_table,
        media_derived_artifacts_table,
        observation_batches_table,
        observation_transformations_table,
    )
    from app.modules.media_processing.analysis.fake_detector import FakeObjectDetector

    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    video_bytes, adapter = _real_ocr_video_fixture()

    try:
        await check_postgres(settings)
        await check_minio(settings)
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/Neo4j/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, _MEDIA_WORKER_TOKEN)

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"ocr-video-live-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105

    graph_driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(graph_driver)
    case_id = None
    user_id = None
    job_id: str | None = None
    try:
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_jobs_table).where(
                    worker_jobs_table.c.processor_name == "media_detection_v1",
                    worker_jobs_table.c.status == "queued",
                )
            )

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            register = await ac.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": password,
                    "display_name": "OCR Video Live Test",
                },
            )
            assert register.status_code == 201, register.text
            user_id = register.json()["user_id"]

            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"OCR-VIDEO-LIVE-{uuid4().hex[:8]}",
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
                files={"file": ("ocr_video_test.mp4", video_bytes, "video/mp4")},
                data={"source_type": "video", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            assert upload.json()["job"]["processor_name"] == "media_detection_v1"

            # --- real worker: claim -> stream -> SHA-256 verify -> sample real
            # frames -> real OCR per frame -> batch submit -> empty terminal result ---
            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
            )
            try:
                outcome = run_once(
                    client=client,
                    input_resolver=LiveInputResolver(client),
                    detector=FakeObjectDetector(),
                    ocr_adapter=adapter,  # type: ignore[arg-type]
                )
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
            assert job_status["observation_count"] > 0

            status_text = status_response.text
            assert "object_uri" not in status_text
            if settings.worker_token:
                assert settings.worker_token.get_secret_value() not in status_text

        # --- durable rows: real, frame-precise, recognizable OCR text ---
        async with ac_engine.begin() as conn:
            obs_rows = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.job_id == uuid4().__class__(job_id)
                        )
                    )
                )
                .mappings()
                .all()
            )
            ocr_rows = [r for r in obs_rows if r["observation_type"] == "ocr_text_mention"]
            assert ocr_rows, "no ocr_text_mention observation was persisted for the video"
            frame_ocr_rows = [
                r
                for r in ocr_rows
                if r["canonical_payload"]["source_locator"].get("frame_number") is not None
            ]
            assert frame_ocr_rows, "no ocr_text_mention observation carries a frame_number locator"
            for row in frame_ocr_rows:
                locator = row["canonical_payload"]["source_locator"]
                assert locator["time_start_ms"] is not None
                assert locator["time_end_ms"] is not None
                assert locator["time_start_ms"] <= locator["time_end_ms"]
                assert locator["bbox_xyxy_normalized"] is not None
            recognized_text = " ".join(
                mention["text"]
                for row in frame_ocr_rows
                for mention in row["canonical_payload"]["extracted_entities"]
            ).upper()
            assert "TRACEX" in recognized_text
            # Terminal result observations are empty -- everything went through batches.
            result_obs = [r for r in obs_rows if r["result_id"] is not None]
            assert result_obs == [], "terminal result must carry no observations"
            observation_ids = [r["observation_id"] for r in obs_rows]

        # --- graph outbox exactly once per observation, projector idempotent ---
        async with ac_engine.begin() as conn:
            outbox_rows = (
                (
                    await conn.execute(
                        sa.select(graph_projection_jobs_table).where(
                            graph_projection_jobs_table.c.observation_id.in_(observation_ids)
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert len(outbox_rows) == len(observation_ids), (
            "expected exactly one graph-projection job per persisted observation"
        )
        assert {r["status"] for r in outbox_rows} == {"queued"}

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

        obs_id_set = set(observation_ids)
        first_own = [a for a in first_summary.attempts if a.job.observation_id in obs_id_set]
        assert all(a.outcome == "succeeded" for a in first_own)
        second_own = [a for a in second_summary.attempts if a.job.observation_id in obs_id_set]
        assert second_own == [], (
            "second projector run must produce no new work for these observations"
        )

        post_page = await list_case_observations(graph_repository, case_id)
        projected_ids = {item.observation.observation_id for item in post_page.items}
        for oid in observation_ids:
            assert oid in projected_ids, f"observation {oid} not found in Neo4j after projection"
    finally:
        if case_id is not None:
            with contextlib.suppress(Exception):
                await graph_repository.write(
                    "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_id)}
                )
        await graph_repository.close()
        async with ac_engine.begin() as conn:
            if case_id is not None:
                # `media_chunk_observations` has no `case_id` of its own and
                # FKs to both `media_chunks` and `worker_observations` --
                # must go first, or a later delete on either parent leaves
                # the transaction aborted and every subsequent statement in
                # it (including ones wrapped in `contextlib.suppress`, which
                # only swallows the *local* exception, not the poisoned
                # transaction state) fails too.
                with contextlib.suppress(Exception):
                    await conn.execute(
                        sa.delete(media_chunk_observations_table).where(
                            media_chunk_observations_table.c.chunk_id.in_(
                                sa.select(media_chunks_table.c.chunk_id).where(
                                    media_chunks_table.c.case_id == case_id
                                )
                            )
                        )
                    )
                for table in [
                    media_derived_artifacts_table,
                    media_checkpoints_table,
                    media_chunks_table,
                    media_chunk_manifests_table,
                    graph_projection_jobs_table,
                    worker_observations_table,
                    observation_batches_table,
                    observation_transformations_table,
                    worker_results_table,
                    worker_jobs_table,
                    evidence_records_table,
                    case_memberships_table,
                ]:
                    with contextlib.suppress(Exception):
                        await conn.execute(sa.delete(table).where(table.c.case_id == case_id))
                await conn.execute(sa.delete(cases_table).where(cases_table.c.case_id == case_id))
            if user_id is not None:
                await conn.execute(sa.delete(users_table).where(users_table.c.user_id == user_id))
        await ac_engine.dispose()


async def test_ocr_batch_replay_is_idempotent_live() -> None:
    """Scenario 29: submitting the same batch twice returns 'replayed', no duplicate rows.

    Self-skips when the live API or PostgreSQL/MinIO are not reachable.
    """
    from app.modules.evidence_lifecycle.repository import (
        observation_batches_table,
        worker_observations_table,
    )
    from app.modules.media_processing.image.decoder import decode_image
    from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
    from app.modules.media_processing.ocr_batching import OcrBatchConfig, iter_image_ocr_batches

    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    png_bytes, adapter = _real_ocr_fixture()
    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, _MEDIA_WORKER_TOKEN)

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"ocr-replay-live-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105
    case_id = None
    user_id = None

    try:
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_jobs_table).where(
                    worker_jobs_table.c.processor_name == "media_detection_v1",
                    worker_jobs_table.c.status == "queued",
                )
            )

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            register = await ac.post(
                "/api/v1/auth/register",
                json={"email": email, "password": password, "display_name": "OCR Replay Test"},
            )
            assert register.status_code == 201, register.text
            user_id = register.json()["user_id"]

            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"OCR-REPLAY-{uuid4().hex[:8]}",
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
                files={"file": ("replay_test.png", png_bytes, "image/png")},
                data={"source_type": "image", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]

            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
            )
            try:
                claim = client.claim(processor_name="media_detection_v1", processor_version="1.0.0")
                assert claim.job is not None and str(claim.job.job_id) == job_id
                claim_token = claim.claim_token
                job = claim.job

                from app.modules.media_processing.input_resolver import LiveInputResolver  # noqa

                resolved = LiveInputResolver(client).resolve(job, claim_token=claim_token)
                image, metadata = decode_image(resolved.data, limits=DEFAULT_MEDIA_LIMITS)

                ocr_results = adapter.run(image, metadata)  # type: ignore[attr-defined]

                batch_config = OcrBatchConfig(batch_size=50, units_total=len(ocr_results))
                all_batches = list(
                    iter_image_ocr_batches(
                        ocr_results,
                        job=job,
                        idempotency_key_prefix=f"{job_id}-replay",
                        batch_config=batch_config,
                        completed_at=datetime.now(UTC),
                    )
                )
                assert all_batches

                # Submit first time → accepted.
                first_submission = all_batches[0]
                r1 = client.submit_batch(
                    job_id=job.job_id, claim_token=claim_token, submission=first_submission
                )
                assert r1.status == "accepted"

                # Submit identical batch a second time → replayed, never duplicated.
                r2 = client.submit_batch(
                    job_id=job.job_id, claim_token=claim_token, submission=first_submission
                )
                assert r2.status == "replayed"

                # Verify: still only one batch row and one observation row per obs.
                obs_ids = [str(o.observation_id) for o in first_submission.observations]
                async with ac_engine.begin() as conn:
                    batch_rows = (
                        (
                            await conn.execute(
                                sa.select(observation_batches_table).where(
                                    observation_batches_table.c.batch_id
                                    == first_submission.batch_id
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if obs_ids:
                        obs_row = (
                            (
                                await conn.execute(
                                    sa.select(worker_observations_table).where(
                                        worker_observations_table.c.observation_id
                                        == uuid4().__class__(obs_ids[0])
                                    )
                                )
                            )
                            .mappings()
                            .all()
                        )
                        assert len(obs_row) == 1, (
                            "idempotent replay must not duplicate observations"
                        )
                assert len(batch_rows) == 1, "idempotent replay must not create extra batch rows"

            finally:
                client.close()

    finally:
        async with ac_engine.begin() as conn:
            if case_id is not None:
                _case_id_uuid = uuid4().__class__(str(case_id))
                for table in [
                    worker_observations_table,
                    observation_batches_table,
                    worker_results_table,
                    worker_jobs_table,
                    evidence_records_table,
                    case_memberships_table,
                ]:
                    with contextlib.suppress(Exception):
                        await conn.execute(sa.delete(table).where(table.c.case_id == _case_id_uuid))
                await conn.execute(
                    sa.delete(cases_table).where(cases_table.c.case_id == _case_id_uuid)
                )
            if user_id is not None:
                await conn.execute(sa.delete(users_table).where(users_table.c.user_id == user_id))
        await ac_engine.dispose()


async def test_ocr_batch_response_has_no_leaked_secrets_live() -> None:
    """Scenario 30: no object_uri, claim token, or raw media bytes leak in any API response.

    Self-skips when the live API or PostgreSQL/MinIO are not reachable.
    """
    from app.modules.media_processing.image.decoder import decode_image
    from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS
    from app.modules.media_processing.ocr_batching import (
        OcrBatchConfig,
        build_terminal_result,
        iter_image_ocr_batches,
    )

    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    png_bytes, adapter = _real_ocr_fixture()
    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    await _ensure_worker_credential(settings, _MEDIA_WORKER_TOKEN)

    ac_engine = create_ac_engine(settings)
    repository = AccessControlRepository(ac_engine)
    email = f"ocr-nosecrets-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105
    case_id = None
    user_id = None

    try:
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_jobs_table).where(
                    worker_jobs_table.c.processor_name == "media_detection_v1",
                    worker_jobs_table.c.status == "queued",
                )
            )

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            register = await ac.post(
                "/api/v1/auth/register",
                json={"email": email, "password": password, "display_name": "No Secrets Test"},
            )
            assert register.status_code == 201, register.text
            user_id = register.json()["user_id"]

            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"OCR-SECRETS-{uuid4().hex[:8]}",
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
                headers={**user_headers, "Idempotency-Key": str(uuid4())},
                files={"file": ("secret_test.png", png_bytes, "image/png")},
                data={"source_type": "image", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]

            client = WorkerApiClient(
                base_url=settings.worker_api_base_url, worker_token=_MEDIA_WORKER_TOKEN
            )
            try:
                claim = client.claim(processor_name="media_detection_v1", processor_version="1.0.0")
                assert claim.job is not None
                claim_token = claim.claim_token
                job = claim.job

                from app.modules.media_processing.input_resolver import LiveInputResolver  # noqa

                resolved = LiveInputResolver(client).resolve(job, claim_token=claim_token)
                image, metadata = decode_image(resolved.data, limits=DEFAULT_MEDIA_LIMITS)

                ocr_results = adapter.run(image, metadata)  # type: ignore[attr-defined]

                batch_config = OcrBatchConfig(batch_size=50)
                for submission in iter_image_ocr_batches(
                    ocr_results,
                    job=job,
                    idempotency_key_prefix=f"{job_id}-nosecrets",
                    batch_config=batch_config,
                    completed_at=datetime.now(UTC),
                ):
                    receipt = client.submit_batch(
                        job_id=job.job_id, claim_token=claim_token, submission=submission
                    )
                    # Receipt body must not echo any claim token.
                    receipt_text = receipt.model_dump_json()
                    assert claim_token not in receipt_text

                terminal = build_terminal_result(
                    job_id=job.job_id,
                    case_id=job.case_id,
                    evidence_id=job.evidence_id,
                    completed_at=datetime.now(UTC),
                )
                ack = client.submit_result(
                    job_id=job.job_id, claim_token=claim_token, result=terminal
                )
                assert ack.status == "succeeded"

            finally:
                client.close()

            # Job status response must contain no object_uri, claim_token, or raw bytes.
            status_response = await ac.get(
                f"/api/v1/cases/{case.case_id}/jobs/{job_id}", headers=user_headers
            )
            assert status_response.status_code == 200
            resp_text = status_response.text
            assert "object_uri" not in resp_text
            assert claim_token not in resp_text  # type: ignore[possibly-undefined]
            # Raw PNG magic bytes must not appear.
            assert b"\x89PNG" not in resp_text.encode()
            if settings.worker_token:
                assert settings.worker_token.get_secret_value() not in resp_text

    finally:
        async with ac_engine.begin() as conn:
            if case_id is not None:
                _case_id_uuid = uuid4().__class__(str(case_id))
                for table_name in [
                    "worker_observations",
                    "observation_batches",
                    "observation_transformations",
                    "worker_progress_events",
                    "worker_results",
                    "worker_jobs",
                    "evidence_records",
                    "case_memberships",
                ]:
                    with contextlib.suppress(Exception):
                        from sqlalchemy import text as sa_text

                        await conn.execute(
                            sa_text(f"DELETE FROM {table_name} WHERE case_id = :cid"),
                            {"cid": str(_case_id_uuid)},
                        )
                with contextlib.suppress(Exception):
                    await conn.execute(
                        sa.delete(cases_table).where(cases_table.c.case_id == _case_id_uuid)
                    )
            if user_id is not None:
                with contextlib.suppress(Exception):
                    await conn.execute(
                        sa.delete(users_table).where(users_table.c.user_id == user_id)
                    )
        await ac_engine.dispose()
