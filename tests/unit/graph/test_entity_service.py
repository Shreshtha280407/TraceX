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


async def test_exact_identifier_match_never_merges_and_needs_no_candidate() -> None:
    """ADR-029: true Tier-1 "exact blocking". Two DIFFERENT observations
    stating the identical phone number get TWO entities -- never one
    merged entity, still the hard "no automatic identity merge" rule --
    but, unlike the pre-blocking behavior this test used to assert, zero
    resolution candidates: an exact Tier-1 identifier match is blocking-
    level certainty, not a fuzzy signal that needs a human-reviewable
    candidate to confirm. See `test_intelligence.py`'s
    `test_exact_identifier_blocking_produces_zero_within_block_candidates_
    regardless_of_n` for the retrieval-layer proof this holds for any
    block size, not just two."""
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
    assert candidates == ()


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
    """Same-block (identical phone) observations now produce zero
    candidates (see `test_exact_identifier_match_never_merges_and_needs_
    no_candidate` above) -- rerunning must still be idempotent: no crash,
    no duplicate entity creation, consistently empty candidates both
    times. Candidate-upsert-not-duplicated semantics for a genuine
    cross-block match are covered separately by `EntityRepository.
    upsert_candidate`'s own repository-level tests."""
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

    assert run_a == ()
    assert run_b == ()
    assert len(repository.entities) == 2
    assert len(repository.candidates) == 0


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
