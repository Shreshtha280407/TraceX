"""Gap-Closure WP-2: entity creation and resolution-candidate generation.

Mirrors `test_intelligence_sourcing.py`'s observation-fixture pattern
exactly. All fixtures are synthetic, invented, and non-sensitive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.graph.entity_service import (
    create_entities_for_case,
    generate_entity_resolution_candidates,
)
from tests.fixtures.graph.fake_entity_repository import FakeEntityRepository

_EXTRACTOR = Extractor(name="fixture", version="1.0.0", config_hash="h", model_version="n/a")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _observation(*, case_id, observation_type: str, phone: str) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=uuid4(),
        observation_type=observation_type,
        extracted_entities=[ExtractedEntityMention(text=phone, entity_type_hint="phone_number")],
        event_time=None,
        attributes={},
        extraction_confidence=0.9,
        source_locator=SourceLocator(page=1, span_start=0, span_end=5),
        extractor=_EXTRACTOR,
        created_at=_NOW,
    )


async def test_entity_created_from_a_single_observation_with_signal() -> None:
    repository = FakeEntityRepository()
    case_id = uuid4()
    observation = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )

    entities = await create_entities_for_case(repository, case_id, [observation], now=_NOW)

    assert len(entities) == 1
    entity = entities[observation.observation_id]
    assert entity.case_id == case_id
    assert entity.created_from_observation_ids == [observation.observation_id]
    assert entity.stable_identifiers == {"phone": "9876543210"}


async def test_entity_creation_is_idempotent_for_the_same_observation() -> None:
    repository = FakeEntityRepository()
    case_id = uuid4()
    observation = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )

    first = await create_entities_for_case(repository, case_id, [observation], now=_NOW)
    second = await create_entities_for_case(repository, case_id, [observation], now=_NOW)

    assert (
        first[observation.observation_id].entity_id == second[observation.observation_id].entity_id
    )
    assert len(repository.entities) == 1


async def test_exact_identifier_produces_a_candidate_never_a_merge() -> None:
    """Two DIFFERENT observations stating the identical phone number get TWO
    entities and a resolution candidate between them -- never one merged
    entity. This is the hard "no automatic identity merge" rule."""
    repository = FakeEntityRepository()
    case_id = uuid4()
    first = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )
    second = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )

    candidates = await generate_entity_resolution_candidates(
        repository, case_id, [first, second], now=_NOW
    )

    assert len(repository.entities) == 2, "an exact identifier match must never auto-merge entities"
    assert len(candidates) == 1
    candidate = candidates[0]
    assert "exact_identifier" in candidate.reasons
    assert {candidate.left_entity_id, candidate.right_entity_id} == {
        e.entity_id for e in repository.entities.values()
    }


async def test_unrelated_observations_produce_no_candidate() -> None:
    repository = FakeEntityRepository()
    case_id = uuid4()
    first = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )
    second = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="1112223333"
    )

    candidates = await generate_entity_resolution_candidates(
        repository, case_id, [first, second], now=_NOW
    )

    assert candidates == ()
    assert len(repository.entities) == 2


async def test_generation_is_idempotent_and_upserts_not_duplicates() -> None:
    repository = FakeEntityRepository()
    case_id = uuid4()
    first = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )
    second = _observation(
        case_id=case_id, observation_type="phone_number_mention", phone="9876543210"
    )
    observations = [first, second]

    run_a = await generate_entity_resolution_candidates(repository, case_id, observations, now=_NOW)
    run_b = await generate_entity_resolution_candidates(repository, case_id, observations, now=_NOW)

    assert len(run_a) == 1
    assert len(run_b) == 1
    assert run_a[0].entity_resolution_candidate_id == run_b[0].entity_resolution_candidate_id
    assert len(repository.candidates) == 1


async def test_observation_with_no_identity_signal_produces_no_entity() -> None:
    repository = FakeEntityRepository()
    case_id = uuid4()
    blank = ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=uuid4(),
        observation_type="phone_number_mention",
        extracted_entities=[],
        event_time=None,
        attributes={},
        extraction_confidence=0.9,
        source_locator=SourceLocator(page=1, span_start=0, span_end=5),
        extractor=_EXTRACTOR,
        created_at=_NOW,
    )

    entities = await create_entities_for_case(repository, case_id, [blank], now=_NOW)

    assert entities == {}
    assert repository.entities == {}
