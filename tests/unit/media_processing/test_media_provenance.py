"""Scenario 9: deterministic observation IDs; scenario 14 (via draft_to_observation)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import SourceLocator
from app.contracts.observation import ObservationV1
from app.modules.media_processing.models import MediaObservationDraft
from app.modules.media_processing.provenance import (
    build_extractor,
    draft_to_observation,
    is_valid_confidence,
    observation_id,
)

CASE_ID = uuid4()
EVIDENCE_ID = uuid4()
LOCATOR = SourceLocator(time_start_ms=1000, time_end_ms=1100)


def test_same_input_yields_same_observation_id() -> None:
    kwargs = {
        "case_id": CASE_ID,
        "evidence_id": EVIDENCE_ID,
        "processor_name": "media_detection_v1",
        "processor_version": "1.0.0",
        "observation_type": "object_detection",
        "locator": LOCATOR,
    }
    assert observation_id(**kwargs) == observation_id(**kwargs)


def test_changed_locator_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        observation_type="object_detection",
        locator=LOCATOR,
    )
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        observation_type="object_detection",
        locator=SourceLocator(time_start_ms=2000, time_end_ms=2100),
    )
    assert base != changed


def test_changed_processor_version_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        observation_type="object_detection",
        locator=LOCATOR,
    )
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_detection_v1",
        processor_version="2.0.0",
        observation_type="object_detection",
        locator=LOCATOR,
    )
    assert base != changed


def test_discriminator_distinguishes_otherwise_identical_locators() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        observation_type="anonymous_track_segment",
        locator=LOCATOR,
        discriminator="track-a",
    )
    other = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        observation_type="anonymous_track_segment",
        locator=LOCATOR,
        discriminator="track-b",
    )
    assert base != other


def test_different_case_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        processor_name="media_metadata_v1",
        processor_version="1.0.0",
        observation_type="media_metadata",
        locator=LOCATOR,
    )
    other = observation_id(
        case_id=uuid4(),
        evidence_id=EVIDENCE_ID,
        processor_name="media_metadata_v1",
        processor_version="1.0.0",
        observation_type="media_metadata",
        locator=LOCATOR,
    )
    assert base != other


def test_build_extractor_config_hash_changes_with_model_version() -> None:
    a = build_extractor(
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        model_version="fake_detector_v1",
    )
    b = build_extractor(
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        model_version="fake_detector_v2",
    )
    assert a.config_hash != b.config_hash
    assert a.model_version == "fake_detector_v1"


def test_draft_to_observation_produces_valid_observationv1() -> None:
    extractor = build_extractor(
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        model_version="fake_detector_v1",
    )
    draft = MediaObservationDraft(
        observation_type="object_detection",
        locator=LOCATOR,
        confidence=0.9,
        attributes={"detected_label": "person"},
    )
    observation = draft_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        draft=draft,
        extractor=extractor,
        created_at=datetime.now(UTC),
    )
    assert isinstance(observation, ObservationV1)
    assert observation.case_id == CASE_ID
    assert observation.evidence_id == EVIDENCE_ID
    assert observation.extracted_entities == []
    assert observation.attributes == {"detected_label": "person"}


def test_draft_with_entity_text_produces_extracted_entity_mention() -> None:
    extractor = build_extractor(
        processor_name="media_detection_v1", processor_version="1.0.0", model_version="fake_ocr_v1"
    )
    draft = MediaObservationDraft(
        observation_type="ocr_text_mention",
        locator=LOCATOR,
        confidence=0.5,
        entity_text="FAKE_OCR_TEXT_10x10",
        entity_type_hint="ocr_text",
    )
    observation = draft_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        draft=draft,
        extractor=extractor,
        created_at=datetime.now(UTC),
    )
    assert len(observation.extracted_entities) == 1
    assert observation.extracted_entities[0].text == "FAKE_OCR_TEXT_10x10"
    assert observation.extracted_entities[0].entity_type_hint == "ocr_text"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, True),
        (1.0, True),
        (0.5, True),
        (-0.01, False),
        (1.01, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
def test_is_valid_confidence(value: float, expected: bool) -> None:
    assert is_valid_confidence(value) is expected
