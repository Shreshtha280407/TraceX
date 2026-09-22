"""Phase 5 final integration acceptance: the whole documented flow, live.

    case and evidence context
    -> durable manifest/chunk planning for a media job
    -> authenticated worker claim
    -> valid visual chunk-scoped publication (+ rejected out-of-scope interval)
    -> validated structured (CDR) observations, one deliberately rejected
    -> Phase 5 sourcing with correlation-ready gating
    -> two-party descriptor retrieval (CDR caller/callee cross-blocking)
    -> candidate scoring and contradiction handling
    -> durable correlation/outbox records
    -> idempotent graph-update replay
    -> authorized case-scoped graph/correlation read (and a denied one)

Every HTTP call here goes through the real running API server -- this test
does not call `app.modules.media_processing.worker.run_once` or any real
OCR/video decoding; it hand-builds the exact wire payloads a real worker
would submit (mirroring `test_full_pipeline_live.py`'s established
pattern), which is sufficient to prove the coordinator-side wiring
(manifest registration, chunk-scoped publication, validation gates,
descriptor extraction, correlation, replay, authorization) this task adds
and fixes. Only synthetic, non-sensitive content -- no private police
data, no Operation Nightfall data.

Self-skips (never fabricates a pass) whenever there's no `.env`, the live
API server isn't reachable, or PostgreSQL/Neo4j specifically aren't
reachable through it -- same pattern as every other live test in this
package.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from dotenv import dotenv_values

from app.contracts.common import BoundingBoxNormalized, Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.contracts.observation_batch import (
    ObservationBatchProgressV1,
    ObservationBatchSubmissionV1,
)
from app.core.config import Settings
from app.core.ids import deterministic_uuid
from app.dependencies.services import check_minio, check_neo4j, check_postgres
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
)
from app.modules.evidence_lifecycle.media_orchestration import (
    ChunkBoundary,
    ChunkSpec,
    MediaChunkPublication,
    build_manifest,
    chunk_identity,
)
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    graph_projection_jobs_table,
    media_checkpoints_table,
    media_chunk_manifests_table,
    media_chunk_observations_table,
    media_chunks_table,
    media_derived_artifacts_table,
    observation_batches_table,
    observation_transformations_table,
    worker_jobs_table,
    worker_observations_table,
    worker_progress_events_table,
    worker_results_table,
)
from app.modules.graph.integration_projector import replay_graph_updates
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.intelligence.pipeline import run_case_correlation_pass
from app.modules.graph.intelligence.projection import make_correlation_projection_handler
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_pg_engine_for_graph
from app.modules.graph.projector import run_batch
from app.modules.graph.repository import Neo4jGraphRepository, create_driver
from tests.fixtures.access_control.factories import make_user_record

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)

_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42not-a-real-decodable-mp4-payload"
_CHUNK_DURATION_MS = 1_000
_VIDEO_DURATION_MS = 2_000
#: The OCR observation's own recognized text -- this is graph-facing by
#: design (an extracted entity mention, exactly like a phone number or a
#: name; see `graph/mapping.py`'s `document_text_mention`/`organisation`
#: precedent that `test_full_pipeline_live.py` already asserts on). It is
#: not "raw source content" in the sense this test's own raw-content
#: check below cares about.
_OCR_RECOGNIZED_TEXT = "STOP-SIGN-TEXT-9f31c2"
#: A chat message's own free-form body -- genuinely raw source content
#: `graph/mapping.py`'s `chat_message` handling deliberately never reads
#: (only `sender`/`participants`/`platform`/etc. are mapped) and must
#: never appear anywhere in graph-facing data.
_RAW_CHAT_BODY_TEXT = "DO-NOT-LEAK-9f31c2"


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    kwargs.setdefault("worker_credential_pepper", None)
    return Settings(_env_file=None, **kwargs)


def _skip_unless_api_reachable(settings: Settings) -> None:
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server not reachable at {settings.worker_api_base_url}: "
            f"{type(exc).__name__}; start it via `docker compose up --build -d` to run this test"
        )


def _ocr_observation(
    *,
    case_id: UUID,
    evidence_id: UUID,
    time_start_ms: int,
    time_end_ms: int,
    correlation_ready: bool,
) -> dict[str, Any]:
    """One visual OCR-text observation, chunk-scoped by its own time window."""
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="ocr_text",
        extracted_entities=[
            ExtractedEntityMention(text=_OCR_RECOGNIZED_TEXT, entity_type_hint="ocr_text"),
        ],
        event_time=None,
        time_window=None,
        location=None,
        attributes={
            "visual_signal_validation": {
                "outcome": "accepted" if correlation_ready else "rejected",
                "reason_codes": [],
                "explanation": "synthetic benchmark probe",
                "extractor_name": "media_detection_v1",
                "extractor_version": "1.0.0",
                "extractor_config_hash": "deadbeef",
                "correlation_ready": correlation_ready,
            }
        },
        extraction_confidence=0.9,
        source_locator=SourceLocator(
            frame_number=1,
            time_start_ms=time_start_ms,
            time_end_ms=time_end_ms,
            bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.3),
        ),
        extractor=Extractor(
            name="media_detection_v1", version="1.0.0", config_hash="deadbeef", model_version="n/a"
        ),
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")


def _cdr_observation(
    *, case_id: UUID, evidence_id: UUID, caller: str, callee: str, accepted: bool
) -> dict[str, Any]:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="cdr_call_record",
        extracted_entities=[],
        event_time=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        time_window=None,
        location=None,
        attributes={
            "caller_number": caller,
            "callee_number": callee,
            "timestamp": "2026-01-01T10:00:00+00:00",
            "source_signal_quality": {
                "outcome": "accepted" if accepted else "rejected",
                "reason_codes": [] if accepted else ["synthetic_benchmark_rejection"],
                "explanation": "synthetic benchmark probe",
                "extractor_name": "cdr_normalization_v1",
                "extractor_version": "1.0.0",
                "extractor_config_hash": "deadbeef",
            },
        },
        extraction_confidence=0.9,
        source_locator=SourceLocator(row=1),
        extractor=Extractor(
            name="cdr_normalization_v1",
            version="1.0.0",
            config_hash="deadbeef",
            model_version="n/a",
        ),
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")


def _chat_observation(*, case_id: UUID, evidence_id: UUID) -> dict[str, Any]:
    """A validated social/chat observation carrying a raw message body that
    `graph/mapping.py`'s `chat_message` handling must never expose."""
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="chat_message",
        extracted_entities=[],
        event_time=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        time_window=None,
        location=None,
        attributes={
            "sender": "Synthetic Sender",
            "platform": "whatsapp",
            "text": _RAW_CHAT_BODY_TEXT,
            "communication_signal_validation": {
                "outcome": "accepted",
                "reason_codes": [],
                "explanation": "synthetic benchmark probe",
                "extractor_name": "generic_social_json_v1",
                "extractor_version": "1.0.0",
                "extractor_config_hash": "deadbeef",
                "correlation_ready": True,
            },
        },
        extraction_confidence=0.9,
        source_locator=SourceLocator(message_id="m1"),
        extractor=Extractor(
            name="generic_social_json_v1",
            version="1.0.0",
            config_hash="deadbeef",
            model_version="n/a",
        ),
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")


async def test_phase5_full_acceptance_flow_against_live_stack() -> None:  # noqa: PLR0915
    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    try:
        await check_postgres(settings)
        await check_minio(settings)
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO/Neo4j not reachable: {type(exc).__name__}")

    pepper = resolve_worker_pepper(settings)
    ac_engine = create_ac_engine(settings)
    ac_repository = AccessControlRepository(ac_engine)

    worker, worker_token = await create_worker_credential(
        ac_repository,
        display_name="live-phase5-acceptance-worker",
        allowed_processor_names=["media_detection_v1"],
        pepper=pepper,
        now=datetime.now(UTC),
    )

    owner_email = f"phase5-owner-{uuid4().hex[:8]}@example.test"
    outsider_email = f"phase5-outsider-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105
    case_id: UUID | None = None
    owner_id: UUID | None = None
    outsider_id: UUID | None = None
    neo4j_driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(neo4j_driver)
    pg_engine = create_pg_engine_for_graph(settings)
    outbox = GraphProjectionOutboxRepository(pg_engine)
    correlation_repository = GraphCorrelationIntegrationRepository(pg_engine)
    try:
        # No public self-registration exists (G5) -- seed users directly
        # against the same live Postgres `ac_repository` already writes to.
        for email in (owner_email, outsider_email):
            await ac_repository.create_user(make_user_record(email_normalized=email))

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            # --- case and evidence context ---
            owner_login = await ac.post(
                "/api/v1/auth/login", json={"email": owner_email, "password": password}
            )
            assert owner_login.status_code == 200, owner_login.text
            owner_headers = {"Authorization": f"Bearer {owner_login.json()['access_token']}"}

            outsider_login = await ac.post(
                "/api/v1/auth/login", json={"email": outsider_email, "password": password}
            )
            assert outsider_login.status_code == 200, outsider_login.text
            outsider_headers = {"Authorization": f"Bearer {outsider_login.json()['access_token']}"}

            me = await ac.get("/api/v1/auth/me", headers=owner_headers)
            owner_id = UUID(me.json()["user"]["user_id"])
            outsider_me = await ac.get("/api/v1/auth/me", headers=outsider_headers)
            outsider_id = UUID(outsider_me.json()["user"]["user_id"])

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"LIVE-PHASE5-ACCEPTANCE-{uuid4().hex[:8]}",
                classification=ClearanceLevel.RESTRICTED,
                status=CaseStatus.OPEN,
                created_at=datetime.now(UTC),
            )
            await ac_repository.create_case(case)
            case_id = case.case_id
            await ac_repository.create_membership(
                CaseMembershipRecord(
                    membership_id=uuid4(),
                    case_id=case.case_id,
                    user_id=owner_id,
                    role=CaseRole.INVESTIGATOR,
                    clearance=ClearanceLevel.RESTRICTED,
                    is_active=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            # `outsider` deliberately never gets a membership row.

            upload = await ac.post(
                f"/api/v1/cases/{case_id}/evidence",
                headers={**owner_headers, "Idempotency-Key": str(uuid4())},
                files={"file": ("clip.mp4", _VIDEO_BYTES, "video/mp4")},
                data={"source_type": "video", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            evidence_id = UUID(upload.json()["evidence"]["evidence_id"])
            assert upload.json()["job"]["processor_name"] == "media_detection_v1"

            # --- authenticated worker claim ---
            claim = await ac.post(
                "/api/v1/internal/worker-jobs/claim",
                headers={"Authorization": f"Bearer {worker_token}"},
                json={"processor_name": "media_detection_v1", "processor_version": "1.0.0"},
            )
            assert claim.status_code == 200, claim.text
            claim_token = claim.json()["claim_token"]
            assert claim_token

            # --- durable manifest/chunk planning, registered by the worker ---
            manifest = build_manifest(
                case_id=case_id,
                evidence_id=evidence_id,
                job_id=UUID(job_id),
                source_type="video",
                processor_name="media_detection_v1",
                processor_version="1.0.0",
                input_object_uri=upload.json()["job"].get("input_object_uri")
                or "internal://claimed",
                configuration_hash="phase5-acceptance-config",
                chunks=(
                    ChunkSpec(
                        index=0,
                        boundary=ChunkBoundary(time_start_ms=0, time_end_ms=_CHUNK_DURATION_MS),
                    ),
                    ChunkSpec(
                        index=1,
                        boundary=ChunkBoundary(
                            time_start_ms=_CHUNK_DURATION_MS, time_end_ms=_VIDEO_DURATION_MS
                        ),
                    ),
                ),
                created_at=datetime.now(UTC),
            )
            manifest_response = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/media-manifest",
                headers={
                    "Authorization": f"Bearer {worker_token}",
                    "X-Claim-Token": claim_token,
                    "Content-Type": "application/json",
                },
                content=manifest.model_dump_json(),
            )
            assert manifest_response.status_code == 200, manifest_response.text
            assert manifest_response.json()["created"] is True

            # --- valid visual chunk-scoped publication (chunk 0) ---
            ocr_observation = _ocr_observation(
                case_id=case_id,
                evidence_id=evidence_id,
                time_start_ms=100,
                time_end_ms=200,
                correlation_ready=True,
            )
            ocr_batch = ObservationBatchSubmissionV1(
                job_id=UUID(job_id),
                case_id=case_id,
                evidence_id=evidence_id,
                batch_id="chunk-0",
                batch_sequence=0,
                idempotency_key="chunk-0",
                observations=[ObservationV1.model_validate(ocr_observation)],
                transformations=[],
                progress=ObservationBatchProgressV1(
                    stage="video_chunk_ocr",
                    units_total=2,
                    units_completed=1,
                    observations_emitted=1,
                    batch_sequence=0,
                    message_code="CHUNK_OCR_EXTRACTED",
                    occurred_at=datetime.now(UTC),
                ),
                submitted_at=datetime.now(UTC),
                is_final_batch=False,
            )
            valid_publication = MediaChunkPublication(
                manifest_id=manifest.manifest_id,
                manifest_hash=manifest.manifest_hash,
                chunk_id=chunk_identity(manifest, 0),
                chunk_index=0,
                batch=ocr_batch,
                checkpoint_id=deterministic_uuid(
                    "phase5_media_checkpoint",
                    str(manifest.manifest_id),
                    str(chunk_identity(manifest, 0)),
                ),
                completed_at=datetime.now(UTC),
            )
            publish_ok = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/media-chunks/publish",
                headers={
                    "Authorization": f"Bearer {worker_token}",
                    "X-Claim-Token": claim_token,
                    "Content-Type": "application/json",
                },
                content=valid_publication.model_dump_json(),
            )
            assert publish_ok.status_code == 200, publish_ok.text
            assert publish_ok.json()["status"] == "accepted"
            ocr_observation_id = UUID(ocr_observation["observation_id"])

            # --- rejected: observation interval outside its declared chunk ---
            out_of_scope_observation = _ocr_observation(
                case_id=case_id,
                evidence_id=evidence_id,
                time_start_ms=1_500,  # inside chunk 1, not chunk 0
                time_end_ms=1_600,
                correlation_ready=True,
            )
            out_of_scope_batch = ocr_batch.model_copy(
                update={
                    "batch_id": "chunk-0-invalid",
                    "idempotency_key": "chunk-0-invalid",
                    "observations": [ObservationV1.model_validate(out_of_scope_observation)],
                }
            )
            out_of_scope_publication = valid_publication.model_copy(
                update={"batch": out_of_scope_batch, "checkpoint_id": uuid4()}
            )
            rejected = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/media-chunks/publish",
                headers={
                    "Authorization": f"Bearer {worker_token}",
                    "X-Claim-Token": claim_token,
                    "Content-Type": "application/json",
                },
                content=out_of_scope_publication.model_dump_json(),
            )
            assert rejected.status_code == 422, rejected.text

            # --- validated structured (CDR) observations, one two-party pair
            #     plus one deliberately-rejected decoy ---
            cdr_first = _cdr_observation(
                case_id=case_id,
                evidence_id=evidence_id,
                caller="+919876500001",
                callee="+919876500002",
                accepted=True,
            )
            cdr_second = _cdr_observation(
                case_id=case_id,
                evidence_id=evidence_id,
                caller="+919876500002",
                callee="+919876500003",
                accepted=True,
            )
            cdr_rejected = _cdr_observation(
                case_id=case_id,
                evidence_id=evidence_id,
                caller="+919000000091",
                callee="+919000000092",
                accepted=False,
            )
            chat_observation = _chat_observation(case_id=case_id, evidence_id=evidence_id)
            structured_batch = ObservationBatchSubmissionV1(
                job_id=UUID(job_id),
                case_id=case_id,
                evidence_id=evidence_id,
                batch_id="structured-0",
                batch_sequence=1,
                idempotency_key="structured-0",
                observations=[
                    ObservationV1.model_validate(cdr_first),
                    ObservationV1.model_validate(cdr_second),
                    ObservationV1.model_validate(cdr_rejected),
                    ObservationV1.model_validate(chat_observation),
                ],
                transformations=[],
                progress=None,
                submitted_at=datetime.now(UTC),
                is_final_batch=False,
            )
            structured_submit = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/observations",
                headers={
                    "Authorization": f"Bearer {worker_token}",
                    "X-Claim-Token": claim_token,
                    "Content-Type": "application/json",
                },
                content=structured_batch.model_dump_json(),
            )
            assert structured_submit.status_code == 200, structured_submit.text
            assert structured_submit.json()["status"] == "accepted"
            assert structured_submit.json()["accepted_observation_count"] == 4

            # --- close the job ---
            final_result = {
                "schema_version": "v1",
                "job_id": job_id,
                "case_id": str(case_id),
                "evidence_id": str(evidence_id),
                "status": "succeeded",
                "observations": [],
                "derived_artifacts": [],
                "checkpoint": None,
                "error": None,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            close = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/result",
                headers={"Authorization": f"Bearer {worker_token}", "X-Claim-Token": claim_token},
                json=final_result,
            )
            assert close.status_code == 200, close.text

        # --- project Evidence/Observation/mention provenance for real ---
        summary = await run_batch(
            outbox,
            graph_repository,
            now=datetime.now(UTC),
            lease_seconds=settings.graph_projection_lease_seconds,
            batch_size=settings.graph_projection_batch_size,
        )
        this_case_attempts = [a for a in summary.attempts if a.job.case_id == case_id]
        assert this_case_attempts, "no graph-projection attempt was claimed for this case"
        assert all(a.outcome == "succeeded" for a in this_case_attempts)

        # --- Phase 5 sourcing + correlation-ready gating + two-party
        #     retrieval + candidate scoring, then durable outbox ---
        receipt = await run_case_correlation_pass(
            correlation_repository, pg_engine, case_id, now=None
        )
        assert receipt is not None, (
            "expected at least one correlation candidate from the two-party CDR pair"
        )
        assert receipt.replayed is False

        # --- idempotent graph-update replay ---
        handler = make_correlation_projection_handler(graph_repository)
        first_replay = await replay_graph_updates(correlation_repository, handler, now=None)
        assert first_replay.succeeded >= 1

        async def _correlation_node_count() -> int:
            rows = await graph_repository.read(
                "MATCH (c:Correlation {case_id: $case_id}) RETURN count(c) AS n",
                {"case_id": str(case_id)},
            )
            return int(rows[0]["n"])

        async def _entity_node_count() -> int:
            rows = await graph_repository.read(
                "MATCH (e:Entity {case_id: $case_id}) RETURN count(e) AS n",
                {"case_id": str(case_id)},
            )
            return int(rows[0]["n"])

        assert await _correlation_node_count() == 1
        # An accepted candidate remains a reviewable candidate -- never an
        # automatic entity merge.
        assert await _entity_node_count() == 0

        # Re-run the ENTIRE correlation + replay pass again: no duplicate
        # observations, candidates, snapshots, outbox rows, or graph nodes.
        async with pg_engine.connect() as conn:
            observation_count_before = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM worker_observations WHERE case_id = :case_id"),
                    {"case_id": case_id},
                )
            ).scalar_one()
            candidate_count_before = (
                await conn.execute(
                    sa.text(
                        "SELECT count(*) FROM correlation_candidate_links WHERE case_id = :case_id"
                    ),
                    {"case_id": case_id},
                )
            ).scalar_one()
            outbox_count_before = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM graph_update_events WHERE case_id = :case_id"),
                    {"case_id": case_id},
                )
            ).scalar_one()

        second_receipt = await run_case_correlation_pass(
            correlation_repository, pg_engine, case_id, now=None
        )
        assert second_receipt is not None
        assert second_receipt.replayed is True
        second_replay = await replay_graph_updates(correlation_repository, handler, now=None)
        assert second_replay.claimed == 0  # already succeeded; nothing left to replay

        async with pg_engine.connect() as conn:
            observation_count_after = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM worker_observations WHERE case_id = :case_id"),
                    {"case_id": case_id},
                )
            ).scalar_one()
            candidate_count_after = (
                await conn.execute(
                    sa.text(
                        "SELECT count(*) FROM correlation_candidate_links WHERE case_id = :case_id"
                    ),
                    {"case_id": case_id},
                )
            ).scalar_one()
            outbox_count_after = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM graph_update_events WHERE case_id = :case_id"),
                    {"case_id": case_id},
                )
            ).scalar_one()
        assert observation_count_after == observation_count_before
        assert candidate_count_after == candidate_count_before
        assert outbox_count_after == outbox_count_before
        assert await _correlation_node_count() == 1  # never duplicated

        # --- authorized case-scoped graph/correlation read ---
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            owner_login = await ac.post(
                "/api/v1/auth/login", json={"email": owner_email, "password": password}
            )
            owner_headers = {"Authorization": f"Bearer {owner_login.json()['access_token']}"}
            outsider_login = await ac.post(
                "/api/v1/auth/login", json={"email": outsider_email, "password": password}
            )
            outsider_headers = {"Authorization": f"Bearer {outsider_login.json()['access_token']}"}

            candidates_response = await ac.get(
                f"/api/v1/cases/{case_id}/graph/candidates", headers=owner_headers
            )
            assert candidates_response.status_code == 200, candidates_response.text
            candidate_items = candidates_response.json()["items"]
            assert len(candidate_items) >= 1
            # Reviewable only: every returned status is a review state, never
            # "verified" or any identity-confirmation status.
            assert all(item["status"] in {"candidate", "needs_review"} for item in candidate_items)

            # --- denied: a user with no case membership at all ---
            denied = await ac.get(
                f"/api/v1/cases/{case_id}/graph/candidates", headers=outsider_headers
            )
            assert denied.status_code == 403, denied.text

            # --- source locators, evidence IDs, and timing survive; raw
            #     OCR text never appears in graph-facing attributes ---
            observations_response = await ac.get(
                f"/api/v1/cases/{case_id}/graph/observations", headers=owner_headers
            )
            assert observations_response.status_code == 200, observations_response.text
            body_text = observations_response.text
            # Raw source content (a chat message's own free-form body) must
            # never appear -- but the OCR observation's recognized text is
            # correctly present, since it is a genuine extracted entity
            # mention, not raw content (see `_OCR_RECOGNIZED_TEXT`'s own
            # docstring above).
            assert _RAW_CHAT_BODY_TEXT not in body_text
            assert _OCR_RECOGNIZED_TEXT in body_text
            ocr_items = [
                item
                for item in observations_response.json()["items"]
                if item["observation_id"] == str(ocr_observation_id)
            ]
            assert len(ocr_items) == 1
            assert ocr_items[0]["evidence_id"] == str(evidence_id)
    finally:
        if case_id is not None:
            with contextlib.suppress(Exception):
                await graph_repository.write(
                    "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_id)}
                )
        await graph_repository.close()
        with contextlib.suppress(Exception):
            await outbox.close()
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_credentials_table).where(
                    worker_credentials_table.c.worker_id == worker.worker_id
                )
            )
            if case_id is not None:
                for table in (
                    "correlation_candidate_links",
                    "correlation_feature_snapshots",
                    "correlation_records",
                    "graph_update_events",
                ):
                    await conn.execute(
                        sa.text(f"DELETE FROM {table} WHERE case_id = :case_id"),  # noqa: S608
                        {"case_id": case_id},
                    )
                await conn.execute(
                    sa.delete(graph_projection_jobs_table).where(
                        graph_projection_jobs_table.c.case_id == case_id
                    )
                )
                # Children of `media_chunks` (FK on `chunk_id`) must go first.
                await conn.execute(
                    sa.delete(media_chunk_observations_table).where(
                        media_chunk_observations_table.c.chunk_id.in_(
                            sa.select(media_chunks_table.c.chunk_id).where(
                                media_chunks_table.c.case_id == case_id
                            )
                        )
                    )
                )
                await conn.execute(
                    sa.delete(media_derived_artifacts_table).where(
                        media_derived_artifacts_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(media_checkpoints_table).where(
                        media_checkpoints_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(media_chunks_table).where(media_chunks_table.c.case_id == case_id)
                )
                await conn.execute(
                    sa.delete(media_chunk_manifests_table).where(
                        media_chunk_manifests_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(observation_transformations_table).where(
                        observation_transformations_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_progress_events_table).where(
                        worker_progress_events_table.c.case_id == case_id
                    )
                )
                await conn.execute(
                    sa.delete(observation_batches_table).where(
                        observation_batches_table.c.case_id == case_id
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
            for user_id in (owner_id, outsider_id):
                if user_id is not None:
                    await conn.execute(
                        sa.delete(users_table).where(users_table.c.user_id == user_id)
                    )
        await ac_engine.dispose()
        await pg_engine.dispose()
