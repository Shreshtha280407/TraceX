"""Full live Phase 4 -> Phase 5 pipeline: real observations -> real submission
-> Nipun's durable outbox -> real Neo4j projection -> real analytics/motif.

Covers scenarios 9-11 and 14 of the Phase 5A brief's required test list:
correlation submission reaches the durable outbox exactly once; duplicate
submission/replay never duplicates candidate/correlation/snapshot/outbox/
graph output; the Neo4j handler uses projection-key idempotency and requires
real Evidence->Observation provenance; analytics/motif outputs are
case-scoped, seeded, and reproducible. Self-skips (never fabricates a pass)
if PostgreSQL/Neo4j aren't reachable -- see `conftest.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa

from app.contracts.observation import ExtractedEntityMention
from app.modules.graph.integration_projector import replay_graph_updates
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.intelligence.pipeline import (
    run_case_analytics_snapshot,
    run_case_correlation_pass,
    run_case_motif_snapshot,
)
from app.modules.graph.intelligence.projection import make_correlation_projection_handler
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.repository import Neo4jGraphRepository
from tests.integration.graph.test_phase5_correlation_integration_live import _insert_observation

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_SHARED_PHONE = "9876543210"


async def _insert_matching_phone_pair(outbox: GraphProjectionOutboxRepository, *, case_id: UUID):
    """Two real, mapped `phone_number_mention` observations sharing one
    normalized phone -- guaranteed to become exactly one retrieval candidate,
    unlike a generic fixture observation type this module's adapter doesn't
    recognize (see `sourcing.py`'s documented mapping scope)."""
    entity = [ExtractedEntityMention(text=_SHARED_PHONE, entity_type_hint="phone_number")]
    first = await _insert_observation(
        outbox,
        case_id=case_id,
        evidence_id=uuid4(),
        observation_type="phone_number_mention",
        extracted_entities=entity,
        attributes={},
    )
    second = await _insert_observation(
        outbox,
        case_id=case_id,
        evidence_id=uuid4(),
        observation_type="phone_number_mention",
        extracted_entities=entity,
        attributes={},
    )
    return first, second


async def _project_evidence_and_observation(
    repository: Neo4jGraphRepository, *, case_id: UUID, evidence_id: UUID, observation_id: UUID
) -> None:
    """The minimal real `Evidence-[:YIELDED_OBSERVATION]->Observation` chain
    `project_correlation_context`'s own provenance guard requires -- without
    it, the handler is a safe, silent no-op by design (see
    `intelligence/projection.py`)."""
    await repository.write(
        "MERGE (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
        "MERGE (o:Observation {case_id: $case_id, observation_id: $observation_id}) "
        "MERGE (e)-[:YIELDED_OBSERVATION]->(o)",
        {
            "case_id": str(case_id),
            "evidence_id": str(evidence_id),
            "observation_id": str(observation_id),
        },
    )


async def _cleanup_correlation_tables(engine, case_id: UUID) -> None:
    async with engine.begin() as conn:
        for table in (
            "graph_update_events",
            "correlation_candidate_links",
            "correlation_feature_snapshots",
            "correlation_records",
        ):
            await conn.execute(
                sa.text(f"DELETE FROM {table} WHERE case_id = :case_id"),  # noqa: S608
                {"case_id": case_id},
            )


async def _count_correlation_nodes(repository: Neo4jGraphRepository, case_id: UUID) -> int:
    rows = await repository.read(
        "MATCH (c:Correlation {case_id: $case_id}) RETURN count(c) AS n",
        {"case_id": str(case_id)},
    )
    return int(rows[0]["n"])


# --- Scenario 9: correlation submission reaches the durable outbox exactly once ---


async def test_correlation_pass_reaches_the_durable_outbox_exactly_once(
    outbox_repository: GraphProjectionOutboxRepository,
    case_id: UUID,
) -> None:
    engine = outbox_repository._engine  # noqa: SLF001
    await _insert_matching_phone_pair(outbox_repository, case_id=case_id)
    correlation_repository = GraphCorrelationIntegrationRepository(engine)

    try:
        receipt = await run_case_correlation_pass(correlation_repository, engine, case_id, now=_NOW)
        assert receipt is not None
        assert receipt.replayed is False
        async with engine.connect() as conn:
            event_count = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM graph_update_events WHERE case_id = :case_id"),
                    {"case_id": case_id},
                )
            ).scalar_one()
        assert event_count == 1
    finally:
        await _cleanup_correlation_tables(engine, case_id)


# --- Scenarios 10-11: replay/projection idempotency + provenance requirement ---


async def test_full_pipeline_submit_replay_project_is_idempotent_end_to_end(
    outbox_repository: GraphProjectionOutboxRepository,
    repository: Neo4jGraphRepository,
    case_id: UUID,
) -> None:
    """The complete Phase 4 -> Phase 5 chain, run twice: real canonical
    observations -> retrieval/scoring -> durable correlation submission ->
    replay -> real Neo4j projection. The second full pass must not create
    any new PostgreSQL row or Neo4j node."""
    engine = outbox_repository._engine  # noqa: SLF001
    first_obs, second_obs = await _insert_matching_phone_pair(outbox_repository, case_id=case_id)
    correlation_repository = GraphCorrelationIntegrationRepository(engine)
    handler = make_correlation_projection_handler(repository)

    try:
        receipt = await run_case_correlation_pass(correlation_repository, engine, case_id, now=_NOW)
        assert receipt is not None
        for observation in (first_obs, second_obs):
            await _project_evidence_and_observation(
                repository,
                case_id=case_id,
                evidence_id=observation.evidence_id,
                observation_id=observation.observation_id,
            )

        first_replay = await replay_graph_updates(correlation_repository, handler, now=_NOW)
        assert first_replay.succeeded == 1
        assert await _count_correlation_nodes(repository, case_id) == 1

        # Re-run the ENTIRE pipeline again against the exact same observations.
        second_receipt = await run_case_correlation_pass(
            correlation_repository, engine, case_id, now=_NOW
        )
        assert second_receipt is not None
        assert second_receipt.replayed is True
        assert second_receipt.correlation.correlation_id == receipt.correlation.correlation_id

        second_replay = await replay_graph_updates(correlation_repository, handler, now=_NOW)
        assert second_replay.claimed == 0  # already succeeded; nothing left queued to replay
        assert await _count_correlation_nodes(repository, case_id) == 1  # never duplicated

        async with engine.connect() as conn:
            correlation_count = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM correlation_records WHERE case_id = :case_id"),
                    {"case_id": case_id},
                )
            ).scalar_one()
        assert correlation_count == 1
    finally:
        await _cleanup_correlation_tables(engine, case_id)


async def test_projection_never_creates_a_correlation_without_real_evidence_provenance(
    outbox_repository: GraphProjectionOutboxRepository,
    repository: Neo4jGraphRepository,
    case_id: UUID,
) -> None:
    """Same submission/replay path, but the Evidence->Observation chain is
    deliberately never projected into Neo4j -- the handler's own provenance
    guard must make this a safe no-op, never a fabricated Correlation node."""
    engine = outbox_repository._engine  # noqa: SLF001
    await _insert_matching_phone_pair(outbox_repository, case_id=case_id)
    correlation_repository = GraphCorrelationIntegrationRepository(engine)
    handler = make_correlation_projection_handler(repository)

    try:
        receipt = await run_case_correlation_pass(correlation_repository, engine, case_id, now=_NOW)
        assert receipt is not None
        # Deliberately skip `_project_evidence_and_observation` this time.
        replay_summary = await replay_graph_updates(correlation_repository, handler, now=_NOW)
        assert replay_summary.succeeded == 1  # the handler itself does not error...
        assert await _count_correlation_nodes(repository, case_id) == 0  # ...but creates nothing
    finally:
        await _cleanup_correlation_tables(engine, case_id)


# --- Scenario 14: analytics/motif outputs are case-scoped, seeded, reproducible ---


async def test_analytics_snapshot_is_reproducible_for_the_same_case_and_seed(
    outbox_repository: GraphProjectionOutboxRepository,
    case_id: UUID,
) -> None:
    engine = outbox_repository._engine  # noqa: SLF001
    await _insert_matching_phone_pair(outbox_repository, case_id=case_id)
    correlation_repository = GraphCorrelationIntegrationRepository(engine)

    try:
        receipt = await run_case_correlation_pass(correlation_repository, engine, case_id, now=_NOW)
        assert receipt is not None

        analytics_a = await run_case_analytics_snapshot(correlation_repository, case_id, seed=0)
        analytics_b = await run_case_analytics_snapshot(correlation_repository, case_id, seed=0)
        # `run_at` is a genuine wall-clock timestamp -- everything else must
        # be bit-for-bit identical for the same case snapshot/seed.
        assert len(analytics_a) == len(analytics_b) == 1
        assert analytics_a[0].configuration_hash == analytics_b[0].configuration_hash
        assert analytics_a[0].graph_snapshot_hash == analytics_b[0].graph_snapshot_hash
        assert analytics_a[0].values == analytics_b[0].values
        assert analytics_a[0].case_id == case_id
    finally:
        await _cleanup_correlation_tables(engine, case_id)


async def test_motif_snapshot_is_reproducible_and_case_scoped(
    outbox_repository: GraphProjectionOutboxRepository,
    case_id: UUID,
) -> None:
    engine = outbox_repository._engine  # noqa: SLF001
    await _insert_matching_phone_pair(outbox_repository, case_id=case_id)

    try:
        motif_a = await run_case_motif_snapshot(engine, case_id)
        motif_b = await run_case_motif_snapshot(engine, case_id)
        assert motif_a == motif_b
        for match in motif_a:
            assert match.case_id == case_id
    finally:
        pass  # this test writes no correlation/outbox rows -- nothing to clean up there
