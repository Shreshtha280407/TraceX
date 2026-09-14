"""Scenarios 4-6: transcript segments emit correct observations, validation, and round-trip."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
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
    assert mention.attributes["transcript_text_length"] == len("hello world")
    assert mention.attributes["transcript_text_sha256"] == sha256(b"hello world").hexdigest()
    assert "text" not in mention.attributes

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
    assert mentions[0].attributes["transcript_text_length"] == len("hello world")


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


# --- parse_transcript_import_payload (Phase 2: the JSON interchange format) -


def _payload_bytes(segments: list[dict[str, object]]) -> bytes:
    return json.dumps({"segments": segments}).encode("utf-8")


def test_parse_transcript_import_payload_valid_json() -> None:
    data = _payload_bytes(
        [
            {
                "start_ms": 0,
                "end_ms": 500,
                "text": "hi",
                "language_hint": "en",
                "confidence": 0.75,
                "source_segment_id": "seg-1",
            }
        ]
    )
    result = transcript_module.parse_transcript_import_payload(data)
    assert result.segments == (
        TranscriptSegmentInput(
            start_ms=0,
            end_ms=500,
            text="hi",
            language_hint="en",
            confidence=0.75,
            source_segment_id="seg-1",
        ),
    )


def test_parse_transcript_import_payload_accepts_missing_optional_language_hint() -> None:
    data = _payload_bytes(
        [{"start_ms": 0, "end_ms": 1, "text": "x", "confidence": 1.0, "source_segment_id": "s"}]
    )
    result = transcript_module.parse_transcript_import_payload(data)
    assert result.segments[0].language_hint is None


def test_parse_transcript_import_payload_rejects_malformed_utf8() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.parse_transcript_import_payload(b"\xff\xfe\x00not utf-8")
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_transcript_import_payload_rejects_invalid_json() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.parse_transcript_import_payload(b"{not json")
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_transcript_import_payload_rejects_missing_top_level_segments() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.parse_transcript_import_payload(json.dumps({"foo": "bar"}).encode())
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_transcript_import_payload_rejects_missing_required_field() -> None:
    data = _payload_bytes([{"start_ms": 0, "end_ms": 1, "text": "x", "confidence": 1.0}])
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.parse_transcript_import_payload(data)
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_transcript_import_payload_rejects_wrong_field_type() -> None:
    data = _payload_bytes(
        [
            {
                "start_ms": "not-an-int",
                "end_ms": 1,
                "text": "x",
                "confidence": 1.0,
                "source_segment_id": "s",
            }
        ]
    )
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.parse_transcript_import_payload(data)
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_transcript_import_payload_still_enforces_timing_validation() -> None:
    """Malformed-shape checks live here; timing/bounds validation still runs downstream."""
    data = _payload_bytes(
        [{"start_ms": 100, "end_ms": 50, "text": "x", "confidence": 1.0, "source_segment_id": "s"}]
    )
    parsed = transcript_module.parse_transcript_import_payload(data)
    with pytest.raises(ProcessingError) as exc_info:
        transcript_module.transcript_segments_to_mentions(parsed.segments)
    assert exc_info.value.code == ErrorCode.INVALID_TRANSCRIPT_SEGMENT
