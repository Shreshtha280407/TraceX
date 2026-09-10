"""Scenarios 13-15: deterministic observation IDs, and every observation validates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import SourceLocator
from app.contracts.observation import ObservationV1
from app.modules.structured_processing.models import RawMention
from app.modules.structured_processing.provenance import mention_to_observation, observation_id
from app.modules.structured_processing.structured.profiles import (
    FIR_REPORT_TEXT_V1,
    GENERIC_JSON_V1,
)

CASE_ID = uuid4()
EVIDENCE_ID = uuid4()
LOCATOR = SourceLocator(page=1, span_start=0, span_end=5)


def test_same_input_yields_same_observation_id() -> None:
    first = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    second = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    assert first == second


def test_changed_locator_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=SourceLocator(page=1, span_start=1, span_end=6),
    )
    assert base != changed


def test_changed_profile_version_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    bumped_profile = replace(FIR_REPORT_TEXT_V1, version="2.0.0")
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=bumped_profile,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    assert base != changed


def test_changed_observation_type_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="email_address_mention",
        locator=LOCATOR,
    )
    assert base != changed


def test_different_case_or_evidence_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    other_case = observation_id(
        case_id=uuid4(),
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        observation_type="phone_number_mention",
        locator=LOCATOR,
    )
    assert base != other_case


def test_mention_to_observation_produces_a_valid_observationv1() -> None:
    mention = RawMention(
        observation_type="phone_number_mention",
        text="9876543210",
        locator=LOCATOR,
        confidence=0.95,
        entity_type_hint="phone_number",
    )
    observation = mention_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=FIR_REPORT_TEXT_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    assert isinstance(observation, ObservationV1)
    assert observation.extracted_entities[0].text == "9876543210"
    assert observation.extractor.name == FIR_REPORT_TEXT_V1.name
    assert observation.extractor.model_version == "n/a"


def test_record_level_mention_has_no_entity_mention() -> None:
    mention = RawMention(
        observation_type="json_scalar_value",
        text="unused for record-level mentions",
        locator=SourceLocator(json_path="$.a"),
        confidence=1.0,
        attributes={"value": 1},
    )
    observation = mention_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=GENERIC_JSON_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    assert observation.extracted_entities == []
    assert observation.attributes == {"value": 1}
