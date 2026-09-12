"""Scenarios 5, 10, 27: deterministic observation IDs, and Extractor/config-hash provenance."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import SourceLocator
from app.contracts.observation import ObservationV1
from app.modules.communication_processing.models import RawMention
from app.modules.communication_processing.provenance import (
    build_extractor,
    mention_to_observation,
    observation_id,
    profile_config_hash,
)
from app.modules.communication_processing.worker import TRANSCRIPT_IMPORT_V1, WHATSAPP_EXPORT_V1

CASE_ID = uuid4()
EVIDENCE_ID = uuid4()
LOCATOR = SourceLocator(message_id="m1")


def test_same_input_yields_same_observation_id() -> None:
    first = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    second = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    assert first == second


def test_changed_locator_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=SourceLocator(message_id="m2"),
    )
    assert base != changed


def test_changed_profile_version_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    bumped_profile = replace(WHATSAPP_EXPORT_V1, version="2.0.0")
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=bumped_profile,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    assert base != changed


def test_changed_observation_type_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    changed = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="phone_number",
        locator=LOCATOR,
    )
    assert base != changed


def test_different_case_or_evidence_changes_observation_id() -> None:
    base = observation_id(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    other_case = observation_id(
        case_id=uuid4(),
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        observation_type="chat_message",
        locator=LOCATOR,
    )
    assert base != other_case


def test_profile_config_hash_changes_when_profile_version_changes() -> None:
    """Scenario 27 (config half): a changed config produces a changed result."""
    base_hash = profile_config_hash(WHATSAPP_EXPORT_V1)
    bumped_hash = profile_config_hash(replace(WHATSAPP_EXPORT_V1, version="2.0.0"))
    assert base_hash != bumped_hash


def test_profile_config_hash_is_stable_for_the_same_profile() -> None:
    assert profile_config_hash(WHATSAPP_EXPORT_V1) == profile_config_hash(WHATSAPP_EXPORT_V1)


def test_mention_to_observation_carries_extractor_provenance() -> None:
    """Scenario 5: extractor/model/config-version identity is on every observation."""
    mention = RawMention(
        observation_type="chat_message",
        text="whatsapp_message",
        locator=LOCATOR,
        confidence=1.0,
        attributes={"platform": "whatsapp"},
    )
    observation = mention_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=WHATSAPP_EXPORT_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    assert isinstance(observation, ObservationV1)
    assert observation.extractor.name == WHATSAPP_EXPORT_V1.name
    assert observation.extractor.version == WHATSAPP_EXPORT_V1.version
    assert observation.extractor.config_hash == profile_config_hash(WHATSAPP_EXPORT_V1)
    # No ML model runs in this module -- see CLAUDE.md and audio/asr_adapter.py.
    assert observation.extractor.model_version == "n/a"


def test_transcript_segment_confidence_passes_through_extractor_unchanged() -> None:
    """Interchange-import profiles never invent or adjust a caller-supplied confidence."""
    mention = RawMention(
        observation_type="transcript_segment",
        text="transcript_segment[0]",
        locator=SourceLocator(time_start_ms=0, time_end_ms=1000, json_path="$.segments[0]"),
        confidence=0.42,
        attributes={"text": "hello", "source_segment_id": "seg-1", "language_hint": None},
    )
    observation = mention_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=TRANSCRIPT_IMPORT_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    assert observation.extraction_confidence == 0.42
    assert observation.extractor.name == TRANSCRIPT_IMPORT_V1.name


def test_build_extractor_never_claims_a_real_model() -> None:
    extractor = build_extractor(WHATSAPP_EXPORT_V1)
    assert extractor.model_version == "n/a"
