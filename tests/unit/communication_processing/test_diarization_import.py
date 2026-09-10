"""Scenarios 7-9: diarization segments emit source-local-only labels, never identities."""

from __future__ import annotations

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
