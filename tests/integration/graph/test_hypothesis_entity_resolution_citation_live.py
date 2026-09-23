"""ADR-031: hypothesis citation of a WP-2 entity-resolution candidate.

Targeted live-DB tests for the second, parallel citation path added to
`HypothesisRepository.create_hypothesis` -- deliberately narrow (direct
repository calls, no HTTP/Neo4j layer) since `test_review_and_hypothesis_
live.py` already covers the full end-to-end HTTP/Neo4j workflow for the
pre-existing `supporting_candidate_ids` path; this file only needs to prove
the new `supporting_entity_resolution_candidate_ids` path behaves
correctly in isolation.

Self-skips (never fabricates a pass) whenever there's no `.env` or
PostgreSQL specifically isn't reachable -- same pattern as every other live
test in this suite. Only synthetic, non-sensitive fixture data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values

from app.core.config import Settings
from app.dependencies.services import check_postgres
from app.modules.graph.entity_models import ENTITY_CANDIDATE_CONFIG_VERSION
from app.modules.graph.entity_repository import EntityRepository, entity_resolution_candidates_table
from app.modules.graph.entity_repository import create_engine as create_entity_engine
from app.modules.graph.hypothesis_models import HypothesisValidationError
from app.modules.graph.hypothesis_repository import HypothesisRepository, create_engine
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_pg_engine
from app.modules.integrity.signing import generate_signing_key_b64
from tests.integration.graph.test_phase5_correlation_integration_live import _insert_observation

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"
_NOW = datetime(2026, 9, 23, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings(**overrides: Any) -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    kwargs.update(overrides)
    kwargs.setdefault("integrity_signing_key", generate_signing_key_b64())
    return Settings(_env_file=None, **kwargs)


async def _insert_entity_resolution_candidate(
    *, outbox: GraphProjectionOutboxRepository, entity_repository: EntityRepository, case_id: UUID
) -> UUID:
    """A real WP-2 candidate row, built through the real chain (two real
    observations -> two real entities -> one candidate referencing them) --
    `entity_resolution_candidates.left_entity_id`/`right_entity_id` carry a
    real foreign-key constraint against `entities`, so a bare synthetic UUID
    is rejected by Postgres itself, not just by application logic."""
    left_observation = await _insert_observation(outbox, case_id=case_id, evidence_id=uuid4())
    right_observation = await _insert_observation(outbox, case_id=case_id, evidence_id=uuid4())
    left_entity, _ = await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_id,
        source_observation_id=left_observation.observation_id,
        entity_type="phone",
        canonical_label="+919999000001",
        aliases=(),
        stable_identifiers={"phone": "+919999000001"},
        created_at=_NOW,
    )
    right_entity, _ = await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_id,
        source_observation_id=right_observation.observation_id,
        entity_type="phone",
        canonical_label="+919999000002",
        aliases=(),
        stable_identifiers={"phone": "+919999000002"},
        created_at=_NOW,
    )
    candidate_id = uuid4()
    async with entity_repository._engine.begin() as conn:  # noqa: SLF001 - live fixture setup
        await conn.execute(
            sa.insert(entity_resolution_candidates_table).values(
                entity_resolution_candidate_id=candidate_id,
                case_id=case_id,
                left_entity_id=left_entity.entity_id,
                right_entity_id=right_entity.entity_id,
                reasons=["local_vector_candidate"],
                identifier_types=[],
                vector_score=0.9,
                contradiction_reasons=[],
                supporting_observation_ids=[
                    str(left_observation.observation_id),
                    str(right_observation.observation_id),
                ],
                config_version=ENTITY_CANDIDATE_CONFIG_VERSION,
                created_at=_NOW,
            )
        )
    return candidate_id


async def test_valid_entity_resolution_candidate_citation_succeeds() -> None:
    settings = _live_settings()
    try:
        await check_postgres(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL not reachable: {type(exc).__name__}")

    outbox = GraphProjectionOutboxRepository(create_pg_engine(settings))
    entity_repository = EntityRepository(create_entity_engine(settings))
    repository = HypothesisRepository(create_engine(settings))
    try:
        case_id = uuid4()
        candidate_id = await _insert_entity_resolution_candidate(
            outbox=outbox, entity_repository=entity_repository, case_id=case_id
        )

        hypothesis, action, is_new = await repository.create_hypothesis(
            case_id=case_id,
            statement="Discovery invocation: these two entities may be the same identity.",
            rationale=None,
            created_by=uuid4(),
            supporting_observation_ids=(),
            supporting_candidate_ids=(),
            supporting_entity_resolution_candidate_ids=(candidate_id,),
            now=_NOW,
        )
        assert is_new
        assert hypothesis.supporting_entity_resolution_candidate_ids == (candidate_id,)
        assert hypothesis.supporting_candidate_ids == ()
        assert action.action.value == "created"
    finally:
        await repository.close()
        await entity_repository.close()
        await outbox._engine.dispose()  # noqa: SLF001 - live fixture teardown


async def test_missing_or_cross_case_entity_resolution_candidate_citation_is_rejected() -> None:
    settings = _live_settings()
    try:
        await check_postgres(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL not reachable: {type(exc).__name__}")

    outbox = GraphProjectionOutboxRepository(create_pg_engine(settings))
    entity_repository = EntityRepository(create_entity_engine(settings))
    repository = HypothesisRepository(create_engine(settings))
    try:
        case_id, other_case_id = uuid4(), uuid4()
        real_candidate_id = await _insert_entity_resolution_candidate(
            outbox=outbox, entity_repository=entity_repository, case_id=other_case_id
        )
        never_existed_id = uuid4()

        for bad_candidate_id in (real_candidate_id, never_existed_id):
            with pytest.raises(HypothesisValidationError):
                await repository.create_hypothesis(
                    case_id=case_id,
                    statement=f"Citing an invalid candidate {bad_candidate_id}.",
                    rationale=None,
                    created_by=uuid4(),
                    supporting_observation_ids=(),
                    supporting_candidate_ids=(),
                    supporting_entity_resolution_candidate_ids=(bad_candidate_id,),
                    now=_NOW,
                )
    finally:
        await repository.close()
        await entity_repository.close()
        await outbox._engine.dispose()  # noqa: SLF001 - live fixture teardown


async def test_both_citation_types_together_persist_correctly() -> None:
    """A hypothesis citing a real observation AND a WP-2 entity-resolution
    candidate at once -- both lists persist and read back independently,
    neither one overwriting or absorbing the other."""
    settings = _live_settings()
    try:
        await check_postgres(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL not reachable: {type(exc).__name__}")

    outbox = GraphProjectionOutboxRepository(create_pg_engine(settings))
    entity_repository = EntityRepository(create_entity_engine(settings))
    repository = HypothesisRepository(create_engine(settings))
    try:
        case_id = uuid4()
        entity_resolution_candidate_id = await _insert_entity_resolution_candidate(
            outbox=outbox, entity_repository=entity_repository, case_id=case_id
        )
        observation = await _insert_observation(outbox, case_id=case_id, evidence_id=uuid4())

        hypothesis, _action, is_new = await repository.create_hypothesis(
            case_id=case_id,
            statement="Citing both a WP-2 candidate and a real observation together.",
            rationale=None,
            created_by=uuid4(),
            supporting_observation_ids=(observation.observation_id,),
            supporting_candidate_ids=(),
            supporting_entity_resolution_candidate_ids=(entity_resolution_candidate_id,),
            now=_NOW,
        )
        assert is_new
        assert hypothesis.supporting_observation_ids == (observation.observation_id,)
        assert hypothesis.supporting_entity_resolution_candidate_ids == (
            entity_resolution_candidate_id,
        )

        reread = await repository.get_hypothesis(case_id, hypothesis.hypothesis_id)
        assert reread is not None
        assert reread.supporting_entity_resolution_candidate_ids == (
            entity_resolution_candidate_id,
        )
        assert reread.supporting_observation_ids == (observation.observation_id,)
    finally:
        await repository.close()
        await entity_repository.close()
        await outbox._engine.dispose()  # noqa: SLF001 - live fixture teardown


async def test_deterministic_id_hash_differs_with_entity_resolution_candidates_populated() -> None:
    """ADR-015 Decision 4's deterministic-ID hash now folds in
    `sorted(supporting_entity_resolution_candidate_ids)` -- an otherwise
    identical hypothesis with vs. without this field must get two
    different `hypothesis_id`s, and each must still be internally
    idempotent (same inputs twice -> same ID, replays)."""
    settings = _live_settings()
    try:
        await check_postgres(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL not reachable: {type(exc).__name__}")

    outbox = GraphProjectionOutboxRepository(create_pg_engine(settings))
    entity_repository = EntityRepository(create_entity_engine(settings))
    repository = HypothesisRepository(create_engine(settings))
    try:
        case_id = uuid4()
        created_by = uuid4()
        candidate_id = await _insert_entity_resolution_candidate(
            outbox=outbox, entity_repository=entity_repository, case_id=case_id
        )
        observation = await _insert_observation(outbox, case_id=case_id, evidence_id=uuid4())

        common_kwargs = {
            "case_id": case_id,
            "statement": "Same statement, only the entity-resolution citation differs.",
            "rationale": None,
            "created_by": created_by,
            "supporting_observation_ids": (observation.observation_id,),
            "supporting_candidate_ids": (),
            "now": _NOW,
        }
        without_field, _a1, is_new_1 = await repository.create_hypothesis(
            **common_kwargs, supporting_entity_resolution_candidate_ids=()
        )
        with_field, _a2, is_new_2 = await repository.create_hypothesis(
            **common_kwargs, supporting_entity_resolution_candidate_ids=(candidate_id,)
        )
        assert is_new_1 and is_new_2
        assert without_field.hypothesis_id != with_field.hypothesis_id

        replay, _a3, is_new_3 = await repository.create_hypothesis(
            **common_kwargs, supporting_entity_resolution_candidate_ids=(candidate_id,)
        )
        assert not is_new_3
        assert replay.hypothesis_id == with_field.hypothesis_id
    finally:
        await repository.close()
        await entity_repository.close()
        await outbox._engine.dispose()  # noqa: SLF001 - live fixture teardown
