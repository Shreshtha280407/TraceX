"""Scenario: the full evidence-to-graph pipeline against a real running API server.

upload evidence -> authenticated worker claim -> secure API evidence stream
-> worker result with a canonical `ObservationV1` (including an
`extracted_entities` mention) -> durable `graph_projection_jobs` row ->
`app.modules.graph.worker`'s projector run -> a real, case-scoped Neo4j
query (via `list_case_observations`, the same function backing
`GET /api/v1/cases/{case_id}/graph/observations`) returns the expected
`Observation`/`EntityMention` projection.

Self-skips (never fabricates a pass), mirroring
`tests/integration/evidence_lifecycle/test_worker_identity_lifecycle_live.py`'s
pattern exactly, whenever there's no `.env`, the live API server isn't
reachable, or PostgreSQL/Neo4j specifically aren't reachable through it.

Uses only synthetic evidence content (a tiny CSV, routed to
`structured_tabular`/`generic_tabular_v1`) and a hand-built `WorkerResultV1`
-- this test proves the graph-projection pipeline, not
`structured_processing`'s real parsing, so the submitted result is
constructed directly rather than requiring a real parse. Provisions one
throwaway worker credential directly via
`app.modules.access_control.worker_credentials` (the exact same functions
the trusted-operator CLI calls); everything created is cleaned up in a
`finally` block, including the worker credential row, the Neo4j nodes
projected under this case, and the `graph_projection_jobs` row.
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

from app.contracts.observation import ExtractedEntityMention
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
    worker_jobs_table,
    worker_observations_table,
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

_CSV_CONTENT = b"name,value\nalpha,1\nbeta,2\n"


def _live_settings() -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    # See tests/integration/evidence_lifecycle/test_worker_identity_lifecycle_live.py
    # for why this must be forced explicitly rather than left to fall back
    # to tests/conftest.py's fake WORKER_CREDENTIAL_PEPPER.
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


def _succeeded_result_payload(
    *, job_id: str, case_id: str, evidence_id: str, observation: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "job_id": job_id,
        "case_id": case_id,
        "evidence_id": evidence_id,
        "status": "succeeded",
        "observations": [observation],
        "derived_artifacts": [],
        "checkpoint": None,
        "error": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }


def _observation_payload(*, case_id: str, evidence_id: str) -> dict[str, Any]:
    observation_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "v1",
        "observation_id": observation_id,
        "case_id": case_id,
        "evidence_id": evidence_id,
        "observation_type": "generic_tabular_row",
        "extracted_entities": [
            ExtractedEntityMention(text="Alpha Corp", entity_type_hint="organisation").model_dump(
                mode="json"
            )
        ],
        "event_time": None,
        "time_window": None,
        "location": None,
        "attributes": {},
        "extraction_confidence": 0.9,
        "source_locator": {"row": 1},
        "extractor": {
            "name": "generic_tabular_v1",
            "version": "1.0.0",
            "config_hash": "deadbeef",
            "model_version": "n/a",
        },
        "created_at": now,
    }, observation_id


async def test_full_evidence_to_graph_pipeline_against_live_stack() -> None:
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

    # Other live tests in this same sandbox/session (evidence_lifecycle,
    # structured_processing) also submit real worker results, which now
    # unconditionally enqueue a `graph_projection_jobs` row too -- and those
    # tests' own cleanup deletes their `worker_observations` row without
    # knowing about this newer table, leaving an orphaned job behind. Sweep
    # those away first so this test's own `claim_batch` call (oldest-first,
    # bounded batch) reliably reaches its own freshly-enqueued job instead
    # of exhausting its batch on stale orphans from earlier test runs.
    async with ac_engine.begin() as conn:
        await conn.execute(
            sa.delete(graph_projection_jobs_table).where(
                graph_projection_jobs_table.c.observation_id.not_in(
                    sa.select(worker_observations_table.c.observation_id)
                )
            )
        )

    worker, worker_token = await create_worker_credential(
        ac_repository,
        display_name="live-full-pipeline-worker",
        allowed_processor_names=["generic_tabular_v1"],
        pepper=pepper,
        now=datetime.now(UTC),
    )

    email = f"live-full-pipeline-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105
    case_id: UUID | None = None
    user_id: UUID | None = None
    observation_id: UUID | None = None
    neo4j_driver = create_driver(settings)
    graph_repository = Neo4jGraphRepository(neo4j_driver)
    try:
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            register = await ac.post(
                "/api/v1/auth/register",
                json={"email": email, "password": password, "display_name": "Live Pipeline Test"},
            )
            assert register.status_code == 201, register.text
            user_id = UUID(register.json()["user_id"])

            login = await ac.post("/api/v1/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            user_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            case = CaseRecord(
                case_id=uuid4(),
                case_reference=f"LIVE-FULL-PIPELINE-{uuid4().hex[:8]}",
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

            # --- upload evidence ---
            upload = await ac.post(
                f"/api/v1/cases/{case_id}/evidence",
                headers={**user_headers, "Idempotency-Key": str(uuid4())},
                files={"file": ("records.csv", _CSV_CONTENT, "text/csv")},
                data={"source_type": "structured_tabular", "classification": "unclassified"},
            )
            assert upload.status_code == 201, upload.text
            job_id = upload.json()["job"]["job_id"]
            evidence_id = upload.json()["evidence"]["evidence_id"]
            assert upload.json()["job"]["processor_name"] == "generic_tabular_v1"

            # --- authenticated worker claim ---
            claim = await ac.post(
                "/api/v1/internal/worker-jobs/claim",
                headers={"Authorization": f"Bearer {worker_token}"},
                json={"processor_name": "generic_tabular_v1", "processor_version": "1.0.0"},
            )
            assert claim.status_code == 200, claim.text
            claim_token = claim.json()["claim_token"]
            assert claim_token

            # --- secure API evidence stream ---
            stream = await ac.get(
                f"/api/v1/internal/worker-jobs/{job_id}/input",
                headers={"Authorization": f"Bearer {worker_token}", "X-Claim-Token": claim_token},
            )
            assert stream.status_code == 200, stream.text
            assert stream.content == _CSV_CONTENT

            # --- worker result with a canonical ObservationV1 ---
            observation_payload, observation_id_str = _observation_payload(
                case_id=str(case_id), evidence_id=evidence_id
            )
            observation_id = UUID(observation_id_str)
            submit = await ac.post(
                f"/api/v1/internal/worker-jobs/{job_id}/result",
                headers={"Authorization": f"Bearer {worker_token}", "X-Claim-Token": claim_token},
                json=_succeeded_result_payload(
                    job_id=job_id,
                    case_id=str(case_id),
                    evidence_id=evidence_id,
                    observation=observation_payload,
                ),
            )
            assert submit.status_code == 200, submit.text

            # --- durable graph projection job exists, queued ---
            async with ac_engine.begin() as conn:
                row = (
                    (
                        await conn.execute(
                            sa.select(graph_projection_jobs_table).where(
                                graph_projection_jobs_table.c.observation_id == observation_id
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            assert row is not None, "no durable graph_projection_jobs row was enqueued"
            assert row["status"] == "queued"
            assert row["case_id"] == case_id

        # --- run the graph projector (--once equivalent, in-process) ---
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

        # Scoped to this test's own job only: the shared live database may
        # also hold `graph_projection_jobs` rows enqueued by other live
        # tests in this same run (every accepted worker result enqueues
        # one, unconditionally) whose own evidence/observation rows those
        # tests have since cleaned up -- this batch can legitimately claim
        # and fail *those* orphaned rows without that being a problem with
        # this test's own job. Only this test's own outcome is asserted on.
        own_attempts = [a for a in summary.attempts if a.job.observation_id == observation_id]
        assert len(own_attempts) == 1, "this test's own projection job was not claimed/attempted"
        assert own_attempts[0].outcome == "succeeded"

        # --- real, case-scoped Neo4j query confirms the projection ---
        page = await list_case_observations(graph_repository, case_id)
        matching = [
            item for item in page.items if item.observation.observation_id == observation_id
        ]
        assert len(matching) == 1, "projected observation not found via list_case_observations"
        projected = matching[0]
        assert str(projected.observation.evidence_id) == evidence_id
        assert len(projected.mentions) == 1
        assert projected.mentions[0].display_label == "Alpha Corp"
        assert projected.mentions[0].mention_type == "organisation"

        # --- and via the real case-scoped read API itself ---
        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:
            graph_response = await ac.get(
                f"/api/v1/cases/{case_id}/graph/observations", headers=user_headers
            )
        assert graph_response.status_code == 200, graph_response.text
        api_items = graph_response.json()["items"]
        api_matching = [i for i in api_items if i["observation_id"] == str(observation_id)]
        assert len(api_matching) == 1
        assert api_matching[0]["mentions"][0]["display_label"] == "Alpha Corp"
    finally:
        # Best-effort, independent cleanup steps: one step's failure must
        # never prevent the others from running (a prior version of this
        # test learned this the hard way -- a Neo4j write-after-close bug
        # here once left orphaned PostgreSQL rows for later test runs to
        # trip over).
        if case_id is not None:
            with contextlib.suppress(Exception):  # best-effort test cleanup
                await graph_repository.write(
                    "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_id)}
                )
        await graph_repository.close()
        async with ac_engine.begin() as conn:
            await conn.execute(
                sa.delete(worker_credentials_table).where(
                    worker_credentials_table.c.worker_id == worker.worker_id
                )
            )
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
