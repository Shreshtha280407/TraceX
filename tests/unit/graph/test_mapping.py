"""Focused deterministic mapping tests for Phase 3 source observations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.graph.mapping import MAPPING_CONFIG_HASH, MappingStatus, map_observation

_EXTRACTOR = Extractor(name="synthetic", version="1", config_hash="fixture", model_version="n/a")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _observation(
    observation_type: str,
    *,
    attributes: dict[str, object] | None = None,
    entities: list[ExtractedEntityMention] | None = None,
    case_id: object | None = None,
    observation_id: object | None = None,
) -> ObservationV1:
    return ObservationV1(
        observation_id=observation_id or uuid4(),
        case_id=case_id or uuid4(),
        evidence_id=uuid4(),
        observation_type=observation_type,
        extracted_entities=entities or [],
        attributes=attributes or {},  # type: ignore[arg-type]
        extraction_confidence=0.9,
        source_locator=SourceLocator(sheet="CDR", row=2),
        extractor=_EXTRACTOR,
        created_at=_NOW,
    )


@pytest.mark.parametrize(
    "observation_type, hint",
    [
        ("phone_number_mention", "phone_number"),
        ("email_address_mention", "email_address"),
        ("ner_entity_mention", "person"),
    ],
)
def test_document_claims_are_deterministic_and_preserve_mapping_metadata(
    observation_type: str, hint: str
) -> None:
    observation = _observation(
        observation_type,
        entities=[ExtractedEntityMention(text="Synthetic label", entity_type_hint=hint)],
    )

    first = map_observation(observation)
    second = map_observation(observation)

    assert first == second
    assert first.status is MappingStatus.APPLIED
    assert first.claims[0].display_label == "Synthetic label"
    assert first.mapping_config_hash == MAPPING_CONFIG_HASH


def test_document_relation_is_a_source_claim_not_an_identity_or_timeless_edge() -> None:
    plan = map_observation(
        _observation(
            "person_contact_association", attributes={"rule": "person_contact_proximity_v1"}
        )
    )

    assert plan.status is MappingStatus.APPLIED
    assert plan.event is None
    assert plan.claims[0].claim_type == "document_relation_claim"


def test_cdr_call_maps_to_time_bounded_event_with_endpoint_claims() -> None:
    plan = map_observation(
        _observation(
            "cdr_call_record",
            attributes={
                "caller_number": "+919876543210",
                "callee_number": "+919123456789",
                "timestamp": "2026-01-01T04:30:00+00:00",
                "duration_seconds": 120.0,
                "call_type": "outgoing",
                "cell_tower_id": "SYNTH-TOWER-9",
            },
        )
    )

    assert plan.status is MappingStatus.APPLIED
    assert plan.event is not None
    assert plan.event.event_type == "cdr_call"
    assert plan.event.event_time == datetime(2026, 1, 1, 4, 30, tzinfo=UTC)
    assert {claim.role for claim in plan.claims} == {"caller", "callee"}
    assert plan.event.properties["duration_seconds"] == 120.0
    assert plan.event.properties["cell_site_claim"] == "SYNTH-TOWER-9"


def test_financial_transaction_preserves_decimal_string_and_event_time() -> None:
    plan = map_observation(
        _observation(
            "financial_transaction_record",
            attributes={
                "sender_account": "SYNTH-SOURCE-001",
                "receiver_account": "SYNTH-DEST-002",
                "amount": "100000.50",
                "currency": "INR",
                "timestamp": "2026-01-01T04:30:00+00:00",
                "direction": "debit",
            },
        )
    )

    assert plan.status is MappingStatus.APPLIED
    assert plan.event is not None
    assert plan.event.event_type == "financial_transaction"
    assert plan.event.properties["amount"] == "100000.50"
    assert not isinstance(plan.event.properties["amount"], float)
    assert {claim.role for claim in plan.claims} == {"source", "destination"}


@pytest.mark.parametrize(
    "observation",
    [
        _observation("unknown_media_observation"),
        _observation("cdr_call_record", attributes={"caller_number": "+919876543210"}),
        _observation(
            "financial_transaction_record",
            attributes={
                "amount": "10.00",
                "currency": "INR",
                "timestamp": "2026-01-01T00:00:00+00:00",
            },
        ),
    ],
)
def test_unsupported_and_incomplete_inputs_have_explicit_safe_outcomes(
    observation: ObservationV1,
) -> None:
    plan = map_observation(observation)

    assert plan.status in {MappingStatus.UNSUPPORTED, MappingStatus.DEFERRED}
    assert plan.reason
    assert plan.claims == ()
    assert plan.event is None


def test_relevant_source_change_changes_plan_identity() -> None:
    case_id = uuid4()
    observation_id = uuid4()
    common = {
        "sender_account": "SYNTH-SOURCE-001",
        "receiver_account": "SYNTH-DEST-002",
        "currency": "INR",
        "timestamp": "2026-01-01T04:30:00+00:00",
    }
    first = map_observation(
        _observation(
            "financial_transaction_record",
            attributes={**common, "amount": "10.00"},
            case_id=case_id,
            observation_id=observation_id,
        )
    )
    second = map_observation(
        _observation(
            "financial_transaction_record",
            attributes={**common, "amount": "11.00"},
            case_id=case_id,
            observation_id=observation_id,
        )
    )

    assert first.event is not None and second.event is not None
    assert first.event.projection_id != second.event.projection_id
