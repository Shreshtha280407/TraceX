"""Scenarios 24-28 (Phase 3, Nipun): observation-batch ingestion against a real
running API server, real PostgreSQL, and the real graph projector/Neo4j.

Self-skips (never fabricates a pass), mirroring
`tests/integration/graph/test_full_pipeline_live.py`'s pattern exactly, whenever
there's no `.env`, the live API server (or its own dependencies) aren't
reachable, or PostgreSQL/Neo4j specifically aren't reachable through it. The
Alembic migration is applied once per session by this package's own
`conftest.py` (`_migrated_database`, autouse) -- covers scenario 24.

Uses only synthetic, non-sensitive evidence content and hand-built
`ObservationBatchSubmissionV1`/`WorkerResultV1` payloads -- this proves the
batch-ingestion/outbox/projection pipeline, not Jasraj's real document
parsing (out of scope for this task).
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

from app.core.config import Settings
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
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    graph_projection_jobs_table,
    observation_batches_table,
    observation_transformations_table,
    worker_jobs_table,
    worker_observations_table,
    worker_progress_events_table,
    worker_results_table,
)
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_pg_engine_for_graph
from app.modules.graph.projector import run_batch
from app.modules.graph.queries import list_case_observations
from app.modules.graph.repository import Neo4jGraphRepository, create_driver

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

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
    """`/healthz` alone only proves the API *process* is alive, not that its own
    dependencies are reachable -- see docs/architecture/phase-3-decisions.md."""
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
        httpx.get(f"{settings.worker_api_base_url}/readyz", timeout=2.0).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(
            f"live API server or its dependencies not reachable at "
            f"{settings.worker_api_base_url}: {type(exc).__name__}; start it via "
            f"`docker compose up --build -d` to run this test"
        )


def _document_observation_payload(
    *, case_id: str, evidence_id: str, page: int, text: str
) -> dict[str, Any]:
    observation_id = str(uuid4())
    return {
        "schema_version": "v1",
        "observation_id": observation_id,
        "case_id": case_id,
        "evidence_id": evidence_id,
        "observation_type": "document_text_mention",
        "extracted_entities": [],
        "event_time": None,
        "time_window": None,
        "location": None,
        "attributes": {},
        "extraction_confidence": 0.9,
        "source_locator": {"page": page, "span_start": 0, "span_end": len(text)},
        "extractor": {
            "name": "fir_report_text_v1",
            "version": "1.0.0",
            "config_hash": "deadbeef",
            "model_version": "n/a",
        },
        "created_at": datetime.now(UTC).isoformat(),
    }, observation_id


def _transformation_payload(
    *,
    job_id: str,
    case_id: str,
    evidence_id: str,
    batch_id: str,
    ordinal: int,
    output_ids: list[str],
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "v1",
        "transformation_id": str(uuid4()),
        "case_id": case_id,
        "evidence_id": evidence_id,
        "job_id": job_id,
        "batch_id": batch_id,
        "ordinal": ordinal,
        "step_name": "pdf_text_extraction",
        "step_version": "1.0.0",
        "config_hash": "deadbeef",
        "model_version": "n/a",
        "input_locator": {"page": ordinal + 1},
        "output_observation_ids": output_ids,
        "derived_artifact_refs": [],
        "status": "succeeded",
        "started_at": now,
        "completed_at": now,
        "safe_metadata": {},
    }


def _batch_payload(
    *,
    job_id: str,
    case_id: str,
    evidence_id: str,
    batch_id: str,
    batch_sequence: int,
    idempotency_key: str,
    observations: list[dict[str, Any]],
    transformations: list[dict[str, Any]],
    units_completed: int,
    units_total: int,
    observations_emitted: int,
    is_final_batch: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "job_id": job_id,
        "case_id": case_id,
        "evidence_id": evidence_id,
        "batch_id": batch_id,
        "batch_sequence": batch_sequence,
        "idempotency_key": idempotency_key,
        "observations": observations,
        "transformations": transformations,
        "progress": {
            "schema_version": "v1",
            "stage": "parsing",
            "units_total": units_total,
            "units_completed": units_completed,
            "observations_emitted": observations_emitted,
            "batch_sequence": batch_sequence,
            "message_code": "PAGE_PARSED",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        "submitted_at": datetime.now(UTC).isoformat(),
        "is_final_batch": is_final_batch,
    }


class _LiveFixture:
    """Shared register/login/case/worker-credential setup + teardown for this file's tests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ac_engine = create_ac_engine(settings)
        self.ac_repository = AccessControlRepository(self.ac_engine)
        self.case_id: UUID | None = None
        self.user_id: UUID | None = None
        self.worker_id: UUID | None = None
        self.user_headers: dict[str, str] = {}
        self.worker_token: str = ""

    async def setup(self, ac: httpx.AsyncClient, *, allowed_processor_names: list[str]) -> None:
        pepper = resolve_worker_pepper(self.settings)
        worker, self.worker_token = await create_worker_credential(
            self.ac_repository,
            display_name="live-observation-batch-worker",
            allowed_processor_names=allowed_processor_names,
            pepper=pepper,
            now=datetime.now(UTC),
        )
        self.worker_id = worker.worker_id

        email = f"live-observation-batch-{uuid4().hex[:8]}@example.test"
        password = "correct-horse-battery-staple"  # noqa: S105
        register = await ac.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "Live Batch Test"},
        )
        assert register.status_code == 201, register.text
        self.user_id = UUID(register.json()["user_id"])
        login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        self.user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        case = CaseRecord(
            case_id=uuid4(),
            case_reference=f"LIVE-OBS-BATCH-{uuid4().hex[:8]}",
            classification=ClearanceLevel.RESTRICTED,
            status=CaseStatus.OPEN,
            created_at=datetime.now(UTC),
        )
        await self.ac_repository.create_case(case)
        self.case_id = case.case_id
        membership = CaseMembershipRecord(
            membership_id=uuid4(),
            case_id=case.case_id,
            user_id=self.user_id,
            role=CaseRole.INVESTIGATOR,
            clearance=ClearanceLevel.RESTRICTED,
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await self.ac_repository.create_membership(membership)

    async def teardown(self) -> None:
        async with self.ac_engine.begin() as conn:
            if self.worker_id is not None:
                await conn.execute(
                    sa.delete(worker_credentials_table).where(
                        worker_credentials_table.c.worker_id == self.worker_id
                    )
                )
            if self.case_id is not None:
                await conn.execute(
                    sa.delete(graph_projection_jobs_table).where(
                        graph_projection_jobs_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_progress_events_table).where(
                        worker_progress_events_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(observation_transformations_table).where(
                        observation_transformations_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_observations_table).where(
                        worker_observations_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(observation_batches_table).where(
                        observation_batches_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_results_table).where(
                        worker_results_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(worker_jobs_table).where(worker_jobs_table.c.case_id == self.case_id)
                )
                await conn.execute(
                    sa.delete(evidence_records_table).where(
                        evidence_records_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(case_memberships_table).where(
                        case_memberships_table.c.case_id == self.case_id
                    )
                )
                await conn.execute(
                    sa.delete(cases_table).where(cases_table.c.case_id == self.case_id)
                )
            if self.user_id is not None:
                await conn.execute(
                    sa.delete(users_table).where(users_table.c.user_id == self.user_id)
                )
        await self.ac_engine.dispose()


async def _upload_and_claim(ac: httpx.AsyncClient, fixture: _LiveFixture) -> tuple[str, str, str]:
    """Uploads a synthetic document and claims its job.

    Returns `(job_id, evidence_id, claim_token)`.
    """
    upload = await ac.post(
        f"/api/v1/cases/{fixture.case_id}/evidence",
        headers={**fixture.user_headers, "Idempotency-Key": str(uuid4())},
        files={"file": ("fir.txt", b"FIR No. 1/2026 -- synthetic test content", "text/plain")},
        data={"source_type": "document", "classification": "unclassified"},
    )
    assert upload.status_code == 201, upload.text
    job_id = upload.json()["job"]["job_id"]
    evidence_id = upload.json()["evidence"]["evidence_id"]
    assert upload.json()["job"]["processor_name"] == "fir_report_text_v1"

    claim = await ac.post(
        "/api/v1/internal/worker-jobs/claim",
        headers={"Authorization": f"Bearer {fixture.worker_token}"},
        json={"processor_name": "fir_report_text_v1", "processor_version": "1.0.0"},
    )
    assert claim.status_code == 200, claim.text
    claim_token = claim.json()["claim_token"]
    assert claim_token
    return job_id, evidence_id, claim_token


# --- Scenarios 25-26: micro-batch -> durable rows -> exactly-once outbox -> --
# --- real graph projector consumes it, no direct ingestion-to-Neo4j write --


async def test_document_micro_batches_reach_graph_outbox_and_project_exactly_once() -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    try:
        await check_postgres(settings)
        await check_minio(settings)
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO/Neo4j not reachable: {type(exc).__name__}")

    fixture = _LiveFixture(settings)
    neo4j_driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(neo4j_driver)
    try:
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            await fixture.setup(ac, allowed_processor_names=["fir_report_text_v1"])
            job_id, evidence_id, claim_token = await _upload_and_claim(ac, fixture)
            case_id_str = str(fixture.case_id)
            auth_headers = {
                "Authorization": f"Bearer {fixture.worker_token}",
                "X-Claim-Token": claim_token,
            }

            # --- micro-batch 1: page 1 ---
            obs1, obs1_id = _document_observation_payload(
                case_id=case_id_str, evidence_id=evidence_id, page=1, text="page one text"
            )
            batch1 = _batch_payload(
                job_id=job_id,
                case_id=case_id_str,
                evidence_id=evidence_id,
                batch_id="page-1",
                batch_sequence=0,
                idempotency_key="idem-page-1",
                observations=[obs1],
                transformations=[
                    _transformation_payload(
                        job_id=job_id,
                        case_id=case_id_str,
                        evidence_id=evidence_id,
                        batch_id="page-1",
                        ordinal=0,
                        output_ids=[obs1_id],
                    )
                ],
                units_completed=1,
                units_total=2,
                observations_emitted=1,
            )
            submit1 = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/observations",
                headers=auth_headers,
                json=batch1,
            )
            assert submit1.status_code == 200, submit1.text
            assert submit1.json()["status"] == "accepted"

            # --- micro-batch 2: page 2 ---
            obs2, obs2_id = _document_observation_payload(
                case_id=case_id_str, evidence_id=evidence_id, page=2, text="page two text"
            )
            batch2 = _batch_payload(
                job_id=job_id,
                case_id=case_id_str,
                evidence_id=evidence_id,
                batch_id="page-2",
                batch_sequence=1,
                idempotency_key="idem-page-2",
                observations=[obs2],
                transformations=[
                    _transformation_payload(
                        job_id=job_id,
                        case_id=case_id_str,
                        evidence_id=evidence_id,
                        batch_id="page-2",
                        ordinal=0,
                        output_ids=[obs2_id],
                    )
                ],
                units_completed=2,
                units_total=2,
                observations_emitted=2,
            )
            submit2 = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/observations",
                headers=auth_headers,
                json=batch2,
            )
            assert submit2.status_code == 200, submit2.text
            assert submit2.json()["progress"]["units_completed"] == 2

            # --- durable rows exist for both batches ---
            async with fixture.ac_engine.begin() as conn:
                batch_rows = (
                    (
                        await conn.execute(
                            sa.select(observation_batches_table).where(
                                observation_batches_table.c.job_id == UUID(job_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                observation_rows = (
                    (
                        await conn.execute(
                            sa.select(worker_observations_table).where(
                                worker_observations_table.c.job_id == UUID(job_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                transformation_rows = (
                    (
                        await conn.execute(
                            sa.select(observation_transformations_table).where(
                                observation_transformations_table.c.job_id == UUID(job_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                progress_rows = (
                    (
                        await conn.execute(
                            sa.select(worker_progress_events_table)
                            .where(worker_progress_events_table.c.job_id == UUID(job_id))
                            .order_by(worker_progress_events_table.c.ordinal.asc())
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(batch_rows) == 2
            assert len(observation_rows) == 2
            assert len(transformation_rows) == 2
            assert [r["units_completed"] for r in progress_rows] == [1, 2]

            # --- exactly-once durable graph outbox handoff ---
            async with fixture.ac_engine.begin() as conn:
                outbox_rows = (
                    (
                        await conn.execute(
                            sa.select(graph_projection_jobs_table).where(
                                graph_projection_jobs_table.c.observation_id.in_(
                                    [UUID(obs1_id), UUID(obs2_id)]
                                )
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(outbox_rows) == 2
            assert {r["status"] for r in outbox_rows} == {"queued"}

            # --- before projection: nothing about these observations in Neo4j yet ---
            # (proves ingestion itself never wrote to Neo4j directly)
            pre_page = await list_case_observations(graph_repository, fixture.case_id)
            pre_ids = {item.observation.observation_id for item in pre_page.items}
            assert UUID(obs1_id) not in pre_ids
            assert UUID(obs2_id) not in pre_ids

        # --- run the real graph projector (--once equivalent, in-process) ---
        pg_engine = create_pg_engine_for_graph(settings)
        outbox = GraphProjectionOutboxRepository(pg_engine)
        try:
            summary = await run_batch(
                outbox,
                graph_repository,
                now=datetime.now(UTC),
                lease_seconds=settings.graph_projection_lease_seconds,
                batch_size=settings.graph_projection_batch_size,
            )
        finally:
            await outbox.close()

        own_attempts = [
            a for a in summary.attempts if a.job.observation_id in {UUID(obs1_id), UUID(obs2_id)}
        ]
        assert len(own_attempts) == 2
        assert all(a.outcome == "succeeded" for a in own_attempts)

        # --- a second projector run is idempotent: no new work claimed for these ---
        outbox2 = GraphProjectionOutboxRepository(pg_engine)
        try:
            summary2 = await run_batch(
                outbox2,
                graph_repository,
                now=datetime.now(UTC),
                lease_seconds=settings.graph_projection_lease_seconds,
                batch_size=settings.graph_projection_batch_size,
            )
        finally:
            await outbox2.close()
        assert not any(
            a.job.observation_id in {UUID(obs1_id), UUID(obs2_id)} for a in summary2.attempts
        )

        # --- after projection: real Neo4j query confirms both observations ---
        post_page = await list_case_observations(graph_repository, fixture.case_id)
        post_ids = {item.observation.observation_id for item in post_page.items}
        assert UUID(obs1_id) in post_ids
        assert UUID(obs2_id) in post_ids

        # --- and via the real case-scoped HTTP graph-read API itself ---
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            graph_response = await ac.get(
                f"/api/v1/cases/{fixture.case_id}/graph/observations",
                headers=fixture.user_headers,
            )
        assert graph_response.status_code == 200, graph_response.text
        api_ids = {item["observation_id"] for item in graph_response.json()["items"]}
        assert obs1_id in api_ids
        assert obs2_id in api_ids
    finally:
        if fixture.case_id is not None:
            with contextlib.suppress(Exception):
                await graph_repository.write(
                    "MATCH (n {case_id: $case_id}) DETACH DELETE n",
                    {"case_id": str(fixture.case_id)},
                )
        await graph_repository.close()
        await fixture.teardown()


# --- Scenario 27: idempotent replay against the live database --------------


async def test_identical_batch_replay_against_live_database_is_idempotent() -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    fixture = _LiveFixture(settings)
    try:
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            await fixture.setup(ac, allowed_processor_names=["fir_report_text_v1"])
            job_id, evidence_id, claim_token = await _upload_and_claim(ac, fixture)
            case_id_str = str(fixture.case_id)
            auth_headers = {
                "Authorization": f"Bearer {fixture.worker_token}",
                "X-Claim-Token": claim_token,
            }
            obs, obs_id = _document_observation_payload(
                case_id=case_id_str, evidence_id=evidence_id, page=1, text="replay test"
            )
            batch = _batch_payload(
                job_id=job_id,
                case_id=case_id_str,
                evidence_id=evidence_id,
                batch_id="replay-batch",
                batch_sequence=0,
                idempotency_key="idem-replay",
                observations=[obs],
                transformations=[],
                units_completed=1,
                units_total=1,
                observations_emitted=1,
            )

            first = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/observations",
                headers=auth_headers,
                json=batch,
            )
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "accepted"

            second = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/observations",
                headers=auth_headers,
                json=batch,
            )
            assert second.status_code == 200, second.text
            assert second.json()["status"] == "replayed"

            async with fixture.ac_engine.begin() as conn:
                batch_rows = (
                    (
                        await conn.execute(
                            sa.select(observation_batches_table).where(
                                observation_batches_table.c.job_id == UUID(job_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                observation_rows = (
                    (
                        await conn.execute(
                            sa.select(worker_observations_table).where(
                                worker_observations_table.c.observation_id == UUID(obs_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                outbox_rows = (
                    (
                        await conn.execute(
                            sa.select(graph_projection_jobs_table).where(
                                graph_projection_jobs_table.c.observation_id == UUID(obs_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(batch_rows) == 1  # still just the one, never duplicated
            assert len(observation_rows) == 1
            assert len(outbox_rows) == 1  # replay enqueued no additional projection job
    finally:
        await fixture.teardown()


# --- Scenario 28: partial batches followed by a final worker-result --------


async def test_partial_batches_then_final_result_preserves_existing_lifecycle() -> None:
    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    try:
        await check_postgres(settings)
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/MinIO not reachable: {type(exc).__name__}")

    fixture = _LiveFixture(settings)
    try:
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            await fixture.setup(ac, allowed_processor_names=["fir_report_text_v1"])
            job_id, evidence_id, claim_token = await _upload_and_claim(ac, fixture)
            case_id_str = str(fixture.case_id)
            auth_headers = {
                "Authorization": f"Bearer {fixture.worker_token}",
                "X-Claim-Token": claim_token,
            }

            obs, obs_id = _document_observation_payload(
                case_id=case_id_str, evidence_id=evidence_id, page=1, text="final lifecycle test"
            )
            batch = _batch_payload(
                job_id=job_id,
                case_id=case_id_str,
                evidence_id=evidence_id,
                batch_id="page-1",
                batch_sequence=0,
                idempotency_key="idem-final-1",
                observations=[obs],
                transformations=[],
                units_completed=1,
                units_total=1,
                observations_emitted=1,
                is_final_batch=True,
            )
            partial = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/observations",
                headers=auth_headers,
                json=batch,
            )
            assert partial.status_code == 200, partial.text

            # The job must still be claimable-status "running" (unaffected by
            # `is_final_batch`, which is metadata only) -- proven indirectly by
            # the terminal `/result` submission below still succeeding exactly
            # like the pre-existing (Phase 2.1) lifecycle.
            result_payload = {
                "schema_version": "v1",
                "job_id": job_id,
                "case_id": case_id_str,
                "evidence_id": evidence_id,
                "status": "succeeded",
                "observations": [],  # already delivered via the batch above
                "derived_artifacts": [],
                "checkpoint": None,
                "error": None,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            final = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/result",
                headers=auth_headers,
                json=result_payload,
            )
            assert final.status_code == 200, final.text
            assert final.json()["status"] == "succeeded"

            job_view = await ac.get(
                f"/api/v1/cases/{fixture.case_id}/jobs/{job_id}", headers=fixture.user_headers
            )
            assert job_view.status_code == 200, job_view.text
            body = job_view.json()
            assert body["status"] == "succeeded"
            # observation_count reflects the batch-delivered observation even
            # though the terminal result itself carried none.
            assert body["observation_count"] == 1
            assert body["latest_progress"]["units_completed"] == 1

            async with fixture.ac_engine.begin() as conn:
                observation_rows = (
                    (
                        await conn.execute(
                            sa.select(worker_observations_table).where(
                                worker_observations_table.c.observation_id == UUID(obs_id)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert len(observation_rows) == 1
            assert observation_rows[0]["observation_batch_id"] is not None
            assert observation_rows[0]["result_id"] is None
    finally:
        await fixture.teardown()
