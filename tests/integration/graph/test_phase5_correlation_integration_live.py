"""Live PostgreSQL coverage for the Phase 5 correlation/outbox boundary.

The surrounding graph integration suite self-skips when local Docker-backed
infrastructure is unavailable; it never substitutes an in-memory database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.modules.access_control.repository import cases_table, users_table
from app.modules.evidence_lifecycle.repository import (
    evidence_records_table,
    worker_jobs_table,
    worker_observations_table,
    worker_results_table,
)
from app.modules.graph.errors import GraphConnectionError
from app.modules.graph.integration_models import (
    CandidateLinkSubmission,
    CorrelationSubmission,
    FeatureSnapshotSubmission,
    GraphUpdateEventStatus,
)
from app.modules.graph.integration_projector import replay_graph_updates
from app.modules.graph.integration_repository import (
    GraphCorrelationIntegrationRepository,
    IntegrationValidationError,
    candidate_links_table,
    correlation_records_table,
    feature_snapshots_table,
    graph_update_events_table,
)
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from tests.fixtures.factories import make_observation

_NOW = datetime(2026, 9, 14, tzinfo=UTC)


async def _ensure_case_and_evidence(conn, *, case_id, evidence_id, user_id) -> None:
    """Idempotently satisfy `worker_observations`' full referential chain
    (`users` -> `cases` -> `evidence_records`) -- this fixture only needs
    *a* valid case/evidence pair to exist, never its own case-CRUD/upload
    behavior, so a direct minimal insert (mirroring this suite's own
    `test_outbox_repository_live.py::_insert_job` style) is used rather
    than the real HTTP upload path another live test already covers.
    """
    await conn.execute(
        postgresql.insert(users_table)
        .values(
            user_id=user_id,
            email_normalized=f"phase5-fixture-{user_id}@example.test",
            display_name="Phase 5 Fixture User",
            password_hash="not-a-real-hash",
            is_active=True,
            created_at=_NOW,
            updated_at=_NOW,
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    await conn.execute(
        postgresql.insert(cases_table)
        .values(
            case_id=case_id,
            case_reference=f"PHASE5-FIXTURE-{case_id}",
            classification="restricted",
            status="open",
            created_at=_NOW,
        )
        .on_conflict_do_nothing(index_elements=["case_id"])
    )
    await conn.execute(
        postgresql.insert(evidence_records_table)
        .values(
            evidence_id=evidence_id,
            case_id=case_id,
            source_type="document",
            original_filename="fixture.txt",
            content_type="text/plain",
            object_uri=f"local://phase5-fixture/{evidence_id}",
            sha256="0" * 64,
            classification="unclassified",
            uploaded_by=user_id,
            uploaded_at=_NOW,
            processing_status="processed",
            created_at=_NOW,
            updated_at=_NOW,
        )
        .on_conflict_do_nothing(index_elements=["evidence_id"])
    )


async def _insert_observation(
    outbox: GraphProjectionOutboxRepository, *, case_id, evidence_id, **observation_overrides
):
    observation = make_observation(
        case_id=case_id, evidence_id=evidence_id, created_at=_NOW, **observation_overrides
    )
    job_id, result_id, user_id = uuid4(), uuid4(), uuid4()
    async with outbox._engine.begin() as conn:  # noqa: SLF001 - live fixture setup
        await _ensure_case_and_evidence(
            conn, case_id=case_id, evidence_id=evidence_id, user_id=user_id
        )
        await conn.execute(
            sa.insert(worker_jobs_table).values(
                job_id=job_id,
                case_id=case_id,
                evidence_id=evidence_id,
                source_type="document",
                processor_name="fixture_processor",
                processor_version="1.0.0",
                attempt=1,
                idempotency_key=f"phase5-fixture-{job_id}",
                input_object_uri=f"local://phase5-fixture/{evidence_id}",
                requested_at=_NOW,
                status="succeeded",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await conn.execute(
            sa.insert(worker_results_table).values(
                result_id=result_id,
                job_id=job_id,
                case_id=case_id,
                evidence_id=evidence_id,
                attempt=1,
                status="succeeded",
                derived_artifacts=[],
                canonical_payload={"job_id": str(job_id), "status": "succeeded"},
                payload_hash="0" * 64,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await conn.execute(
            sa.insert(worker_observations_table).values(
                observation_id=observation.observation_id,
                # `worker_observations`'s `ck_worker_observations_exactly_one_source`
                # check constraint requires exactly one of these two, and
                # `worker_observations_result_id_fkey` requires it to
                # reference a real `worker_results` row -- both satisfied
                # by the terminal-result chain inserted just above.
                result_id=result_id,
                observation_batch_id=None,
                job_id=job_id,
                case_id=case_id,
                evidence_id=evidence_id,
                observation_type=observation.observation_type,
                canonical_payload=observation.model_dump(mode="json"),
                created_at=_NOW,
            )
        )
    return observation


async def _cleanup(outbox: GraphProjectionOutboxRepository, case_id) -> None:
    async with outbox._engine.begin() as conn:  # noqa: SLF001 - live cleanup
        await conn.execute(
            sa.delete(graph_update_events_table).where(
                graph_update_events_table.c.case_id == case_id
            )
        )
        await conn.execute(
            sa.delete(feature_snapshots_table).where(feature_snapshots_table.c.case_id == case_id)
        )
        await conn.execute(
            sa.delete(candidate_links_table).where(candidate_links_table.c.case_id == case_id)
        )
        await conn.execute(
            sa.delete(correlation_records_table).where(
                correlation_records_table.c.case_id == case_id
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
        # `cases`/`users` rows are this fixture's own synthetic scaffolding
        # (never shared with real user/case data) -- read the uploader
        # *before* deleting `evidence_records`, then delete
        # evidence/case/user in FK-safe order.
        uploader_ids = (
            (
                await conn.execute(
                    sa.select(evidence_records_table.c.uploaded_by).where(
                        evidence_records_table.c.case_id == case_id
                    )
                )
            )
            .scalars()
            .all()
        )
        await conn.execute(
            sa.delete(evidence_records_table).where(evidence_records_table.c.case_id == case_id)
        )
        await conn.execute(sa.delete(cases_table).where(cases_table.c.case_id == case_id))
        if uploader_ids:
            await conn.execute(
                sa.delete(users_table).where(users_table.c.user_id.in_(uploader_ids))
            )


async def test_correlation_write_is_atomic_idempotent_and_replayable_after_graph_outage(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    case_id, evidence_id = uuid4(), uuid4()
    repository = GraphCorrelationIntegrationRepository(outbox_repository._engine)  # noqa: SLF001
    first = await _insert_observation(outbox_repository, case_id=case_id, evidence_id=evidence_id)
    second = await _insert_observation(outbox_repository, case_id=case_id, evidence_id=evidence_id)
    submission = CorrelationSubmission(
        idempotency_key="phase5.live.correlation",
        correlation_type="fixture_proposition",
        supporting_observation_ids=(first.observation_id, second.observation_id),
        candidate_links=(
            CandidateLinkSubmission(
                idempotency_key="phase5.live.candidate",
                left_observation_id=first.observation_id,
                right_observation_id=second.observation_id,
            ),
        ),
        feature_snapshot=FeatureSnapshotSubmission(
            snapshot_version="fixture.v1", config_hash="fixture-config", values={"bounded": True}
        ),
        mapping_version="mapping.v1",
        config_version="config.v1",
    )
    try:
        receipt = await repository.submit(case_id=case_id, submission=submission, now=_NOW)
        replayed = await repository.submit(case_id=case_id, submission=submission, now=_NOW)
        assert receipt.replayed is False
        assert replayed.replayed is True
        assert replayed.correlation.correlation_id == receipt.correlation.correlation_id
        assert receipt.event.status is GraphUpdateEventStatus.QUEUED
        assert len(receipt.correlation.evidence_paths) == 2

        async with outbox_repository._engine.connect() as conn:  # noqa: SLF001
            assert (
                await conn.execute(
                    sa.select(sa.func.count()).select_from(correlation_records_table)
                )
            ).scalar_one() >= 1
            event_count = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(graph_update_events_table)
                    .where(graph_update_events_table.c.case_id == case_id)
                )
            ).scalar_one()
        assert event_count == 1

        async def unavailable(_) -> None:
            raise GraphConnectionError("neo4j unavailable")

        outage = await replay_graph_updates(repository, unavailable, now=_NOW)
        assert outage.retrying == 1
        assert (
            await repository.get_event(case_id, receipt.event.event_id)
        ).status is GraphUpdateEventStatus.QUEUED  # type: ignore[union-attr]

        projection_keys: set[str] = set()

        async def available(context) -> None:
            projection_keys.add(context.event.projection_key)

        recovered = await replay_graph_updates(repository, available, now=_NOW)
        assert recovered.succeeded == 1
        assert projection_keys == {receipt.event.projection_key}
    finally:
        await _cleanup(outbox_repository, case_id)


async def test_cross_case_observation_reference_is_rejected_without_any_durable_write(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    case_id, other_case_id, evidence_id = uuid4(), uuid4(), uuid4()
    repository = GraphCorrelationIntegrationRepository(outbox_repository._engine)  # noqa: SLF001
    local = await _insert_observation(outbox_repository, case_id=case_id, evidence_id=evidence_id)
    foreign = await _insert_observation(
        outbox_repository, case_id=other_case_id, evidence_id=uuid4()
    )
    try:
        with pytest.raises(IntegrationValidationError, match="missing or outside this case"):
            await repository.submit(
                case_id=case_id,
                submission=CorrelationSubmission(
                    idempotency_key="phase5.cross-case",
                    correlation_type="fixture_proposition",
                    supporting_observation_ids=(local.observation_id, foreign.observation_id),
                    mapping_version="mapping.v1",
                    config_version="config.v1",
                ),
                now=_NOW,
            )
        assert await repository.list_correlations(case_id) == []
    finally:
        await _cleanup(outbox_repository, case_id)
        await _cleanup(outbox_repository, other_case_id)
