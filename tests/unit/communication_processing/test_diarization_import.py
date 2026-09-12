"""Scenarios 7-9: diarization segments emit source-local-only labels, never identities."""

from __future__ import annotations

import json

import pytest

from app.modules.communication_processing.audio import diarization_import as diarization_module
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import DiarizationSegmentInput


def _segment(**overrides: object) -> DiarizationSegmentInput:
    data: dict[str, object] = {
        "start_ms": 0,
        "end_ms": 1000,
        "speaker_label": "SPEAKER_00",
        "confidence": 0.9,
        "source_segment_id": "seg-1",
    }
    data.update(overrides)
    return DiarizationSegmentInput(**data)  # type: ignore[arg-type]


def test_valid_segment_emits_source_local_label_only() -> None:
    mentions = diarization_module.diarization_segments_to_mentions((_segment(),))
    assert len(mentions) == 1
    mention = mentions[0]
    assert mention.observation_type == "diarization_speaker_turn"
    assert mention.text == "SPEAKER_00"
    assert mention.entity_type_hint == "speaker_label_local"
    assert mention.locator.time_start_ms == 0
    assert mention.locator.time_end_ms == 1000


def test_speaker_label_never_becomes_a_verified_identity_hint() -> None:
    """The entity_type_hint must never claim verified identity -- only source-local label."""
    mentions = diarization_module.diarization_segments_to_mentions((_segment(),))
    assert mentions[0].entity_type_hint == "speaker_label_local"


def test_imported_segment_is_tagged_metadata_supplied_not_model_derived() -> None:
    """Section 5.3: an externally-imported segment must never be indistinguishable
    from one a local model genuinely derived -- no adapter in this phase ever
    produces `model_derived` (see `audio/diarization_adapter.py`)."""
    mentions = diarization_module.diarization_segments_to_mentions((_segment(),))
    assert mentions[0].attributes["segment_source"] == "metadata_supplied"
    assert "verified" not in (mentions[0].entity_type_hint or "")
    assert "person" not in (mentions[0].entity_type_hint or "")


def test_overlapping_turns_are_preserved_not_discarded() -> None:
    overlapping = (
        _segment(start_ms=0, end_ms=1000, speaker_label="SPEAKER_00", source_segment_id="a"),
        _segment(start_ms=500, end_ms=1500, speaker_label="SPEAKER_01", source_segment_id="b"),
    )
    mentions = diarization_module.diarization_segments_to_mentions(overlapping)
    assert len(mentions) == 2  # both kept, never merged/deduplicated


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_ms": -1},
        {"end_ms": 0, "start_ms": 0},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"speaker_label": "   "},
        {"source_segment_id": ""},
    ],
)
def test_invalid_segment_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.diarization_segments_to_mentions((_segment(**overrides),))
    assert exc_info.value.code == ErrorCode.INVALID_DIARIZATION_SEGMENT


def test_overlong_speaker_label_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diarization_module, "MAX_SPEAKER_LABEL_LENGTH", 3)
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.diarization_segments_to_mentions(
            (_segment(speaker_label="TOO_LONG_LABEL"),)
        )
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_too_many_segments_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diarization_module, "MAX_DIARIZATION_SEGMENTS", 1)
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.diarization_segments_to_mentions(
            (_segment(), _segment(source_segment_id="seg-2"))
        )
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


# --- parse_diarization_import_payload (Phase 2: the JSON interchange format) -


def _payload_bytes(segments: list[dict[str, object]]) -> bytes:
    return json.dumps({"segments": segments}).encode("utf-8")


def test_parse_diarization_import_payload_valid_json() -> None:
    data = _payload_bytes(
        [
            {
                "start_ms": 0,
                "end_ms": 500,
                "speaker_label": "SPEAKER_00",
                "confidence": 0.6,
                "source_segment_id": "seg-1",
            }
        ]
    )
    result = diarization_module.parse_diarization_import_payload(data)
    assert result.segments == (
        DiarizationSegmentInput(
            start_ms=0,
            end_ms=500,
            speaker_label="SPEAKER_00",
            confidence=0.6,
            source_segment_id="seg-1",
        ),
    )


def test_parse_diarization_import_payload_rejects_malformed_utf8() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.parse_diarization_import_payload(b"\xff\xfe\x00not utf-8")
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_diarization_import_payload_rejects_invalid_json() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.parse_diarization_import_payload(b"{not json")
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_diarization_import_payload_rejects_missing_top_level_segments() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.parse_diarization_import_payload(json.dumps({"foo": "bar"}).encode())
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_diarization_import_payload_rejects_missing_required_field() -> None:
    data = _payload_bytes([{"start_ms": 0, "end_ms": 1, "confidence": 1.0}])
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.parse_diarization_import_payload(data)
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_diarization_import_payload_rejects_wrong_field_type() -> None:
    data = _payload_bytes(
        [
            {
                "start_ms": 0,
                "end_ms": "not-an-int",
                "speaker_label": "SPEAKER_00",
                "confidence": 1.0,
                "source_segment_id": "s",
            }
        ]
    )
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.parse_diarization_import_payload(data)
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON_PAYLOAD


def test_parse_diarization_import_payload_still_enforces_timing_validation() -> None:
    data = _payload_bytes(
        [
            {
                "start_ms": 100,
                "end_ms": 50,
                "speaker_label": "SPEAKER_00",
                "confidence": 1.0,
                "source_segment_id": "s",
            }
        ]
    )
    parsed = diarization_module.parse_diarization_import_payload(data)
    with pytest.raises(ProcessingError) as exc_info:
        diarization_module.diarization_segments_to_mentions(parsed.segments)
    assert exc_info.value.code == ErrorCode.INVALID_DIARIZATION_SEGMENT
