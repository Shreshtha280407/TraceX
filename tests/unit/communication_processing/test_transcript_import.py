"""Scenarios 4-6: transcript segments emit correct observations, validation, and round-trip."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.observation import ObservationV1
from app.modules.communication_processing.audio import transcript_import as transcript_module
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import TranscriptSegmentInput
from app.modules.communication_processing.provenance import mention_to_observation
from app.modules.communication_processing.worker import TRANSCRIPT_IMPORT_V1


def _segment(**overrides: object) -> TranscriptSegmentInput:
    data: dict[str, object] = {
        "start_ms": 0,
        "end_ms": 1000,
        "text": "hello world",
        "language_hint": "en",
        "confidence": 0.9,
        "source_segment_id": "seg-1",
    }
    data.update(overrides)
    return TranscriptSegmentInput(**data)  # type: ignore[arg-type]


def test_valid_segment_produces_correct_provenance_and_observation() -> None:
    mentions = transcript_module.transcript_segments_to_mentions((_segment(),))
    assert len(mentions) == 1
    mention = mentions[0]
    assert mention.locator.time_start_ms == 0
    assert mention.locator.time_end_ms == 1000
    assert mention.locator.json_path == "$.segments[0]"
    assert mention.confidence == 0.9
    assert mention.attributes["text"] == "hello world"

    observation = mention_to_observation(
        case_id=uuid4(),
        evidence_id=uuid4(),
        profile=TRANSCRIPT_IMPORT_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    assert isinstance(observation, ObservationV1)
    assert observation.observation_type == "transcript_segment"


def test_language_hint_normalized_not_translated() -> None:
    mentions = transcript_module.transcript_segments_to_mentions(
        (_segment(language_hint="  EN-US "),)
    )
    assert mentions[0].attributes["language_hint"] == "en-us"
    assert mentions[0].attributes["text"] == "hello world"  # never translated


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_ms": -1},
        {"end_ms": 0, "start_ms": 0},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"source_segment_id": "   "},
    ],
)
def test_invalid_segment_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.transcript_segments_to_mentions((_segment(**overrides),))
    assert exc_info.value.code == ErrorCode.INVALID_TRANSCRIPT_SEGMENT


def test_overlong_text_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_module, "MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH", 5)
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.transcript_segments_to_mentions((_segment(text="way too long"),))
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_too_many_segments_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_module, "MAX_TRANSCRIPT_SEGMENTS", 1)
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.transcript_segments_to_mentions(
            (_segment(), _segment(source_segment_id="seg-2"))
        )
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_overlong_segment_duration_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_module, "MAX_TRANSCRIPT_SEGMENT_DURATION_MS", 500)
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.transcript_segments_to_mentions((_segment(start_ms=0, end_ms=1000),))
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_observation_round_trips_through_frozen_contract() -> None:
    mention = transcript_module.transcript_segments_to_mentions((_segment(),))[0]
    observation = mention_to_observation(
        case_id=uuid4(),
        evidence_id=uuid4(),
        profile=TRANSCRIPT_IMPORT_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    reloaded = ObservationV1.model_validate_json(observation.model_dump_json())
    assert reloaded == observation
