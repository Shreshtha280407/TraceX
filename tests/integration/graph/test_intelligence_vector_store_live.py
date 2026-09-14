"""Live pgvector coverage for `PgvectorCandidateStore`.

Proves what a fake engine (`tests/unit/graph/test_intelligence_vector_store.py`)
fundamentally can't: the real `CAST(:embedding AS vector)`/`<=>` distance
operator syntax actually executes against the real `vector` extension
(`graph_intelligence_vectors`, migration `e4f7a8b9c0d1`). Self-skips (never
fabricates a pass) if PostgreSQL isn't reachable -- same pattern as the rest
of this package. Confirms this database's PostgreSQL environment genuinely
supports pgvector, per the Phase 5A brief's explicit "first verify whether
the configured PostgreSQL environment supports the required pgvector
extension" requirement.
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.graph.intelligence.models import ObservationDescriptor
from app.modules.graph.intelligence.vector_store import PgvectorCandidateStore
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository


def _descriptor(case_id: object, *, aliases: tuple[str, ...]) -> ObservationDescriptor:
    return ObservationDescriptor(
        case_id=case_id,  # type: ignore[arg-type]
        observation_id=uuid4(),
        evidence_id=uuid4(),
        source_locator_reference="message_id=m1",
        aliases=aliases,
    )


async def _cleanup(engine: AsyncEngine, case_id: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("DELETE FROM graph_intelligence_vectors WHERE case_id = :case_id"),
            {"case_id": case_id},
        )


async def test_pgvector_extension_is_available_in_this_environment(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    """The clearest possible proof the configured PostgreSQL supports pgvector:
    the extension is actually installed, not merely declared in a migration."""
    async with outbox_repository._engine.connect() as conn:  # noqa: SLF001
        rows = (
            await conn.execute(sa.text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
        ).all()
    assert len(rows) == 1, "pgvector extension is not installed in this PostgreSQL instance"


async def test_upsert_then_search_finds_a_similar_same_case_observation(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    engine = outbox_repository._engine  # noqa: SLF001
    store = PgvectorCandidateStore(engine)
    case_id = uuid4()
    try:
        alice = _descriptor(case_id, aliases=("Alice Kumar",))
        alice_again = _descriptor(case_id, aliases=("Alice Kumar",))
        unrelated = _descriptor(case_id, aliases=("Bob Singh",))
        await store.upsert(alice)
        await store.upsert(alice_again)
        await store.upsert(unrelated)

        results = await store.search(alice)
        result_ids = {observation_id for observation_id, _score in results}
        assert alice_again.observation_id in result_ids
        # The near-identical alias scores at least as high as the unrelated one.
        scores = dict(results)
        if unrelated.observation_id in scores:
            assert scores[alice_again.observation_id] >= scores[unrelated.observation_id]
    finally:
        await _cleanup(engine, case_id)


async def test_search_never_returns_a_different_cases_observation(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    """Case isolation at the SQL level, not just application logic."""
    engine = outbox_repository._engine  # noqa: SLF001
    store = PgvectorCandidateStore(engine)
    case_a, case_b = uuid4(), uuid4()
    try:
        in_case_a = _descriptor(case_a, aliases=("Alice Kumar",))
        in_case_b = _descriptor(case_b, aliases=("Alice Kumar",))
        await store.upsert(in_case_a)
        await store.upsert(in_case_b)

        results = await store.search(in_case_a)
        result_ids = {observation_id for observation_id, _score in results}
        assert in_case_b.observation_id not in result_ids
    finally:
        await _cleanup(engine, case_a)
        await _cleanup(engine, case_b)


async def test_upsert_is_idempotent_for_the_same_case_and_observation(
    outbox_repository: GraphProjectionOutboxRepository,
) -> None:
    engine = outbox_repository._engine  # noqa: SLF001
    store = PgvectorCandidateStore(engine)
    case_id = uuid4()
    try:
        descriptor = _descriptor(case_id, aliases=("Alice Kumar",))
        first_id = await store.upsert(descriptor)
        second_id = await store.upsert(descriptor)
        assert first_id == second_id

        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    sa.text(
                        "SELECT count(*) FROM graph_intelligence_vectors "
                        "WHERE case_id = :case_id AND observation_id = :observation_id"
                    ),
                    {"case_id": case_id, "observation_id": descriptor.observation_id},
                )
            ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(engine, case_id)
