"""Typed import of externally-produced diarization (speaker-turn) segments.

This module never runs real diarization, speaker verification, or voice
biometrics. `speaker_label` is source-local only (e.g. `SPEAKER_00`,
`unknown_speaker_1`) — a label meaningful only within the one evidence
file it came from. It is never treated as, or converted into, a verified
person, an `EntityV1`, or a cross-evidence identity, and this module never
combines speaker labels across two different evidence files (each call
processes exactly one evidence file's segments, statelessly).

Overlapping turns (two segments whose time ranges overlap, which real
diarization output legitimately produces for cross-talk) are preserved
exactly as supplied — never merged, deduplicated, or silently dropped.
"""

from __future__ import annotations

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_DIARIZATION_SEGMENTS,
    MAX_SPEAKER_LABEL_LENGTH,
)
from app.modules.communication_processing.models import DiarizationSegmentInput, RawMention

OBSERVATION_TYPE = "diarization_speaker_turn"
ENTITY_TYPE_HINT = "speaker_label_local"


def validate_diarization_segments(
    segments: tuple[DiarizationSegmentInput, ...],
) -> tuple[DiarizationSegmentInput, ...]:
    """Validate segment bounds; raise on the first violation.

    Rules: `start_ms >= 0`; `end_ms > start_ms`; confidence in `[0.0,
    1.0]`; non-empty `speaker_label` bounded by `MAX_SPEAKER_LABEL_LENGTH`;
    non-empty `source_segment_id`. Overlapping turns are not an error —
    they are a legitimate, expected diarization output and are preserved.
    """
    if len(segments) > MAX_DIARIZATION_SEGMENTS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"diarization exceeds the {MAX_DIARIZATION_SEGMENTS}-segment limit",
        )

    for index, segment in enumerate(segments):
        if segment.start_ms < 0:
            raise ProcessingError(
                ErrorCode.INVALID_DIARIZATION_SEGMENT, f"segment {index} has start_ms < 0"
            )
        if segment.end_ms <= segment.start_ms:
            raise ProcessingError(
                ErrorCode.INVALID_DIARIZATION_SEGMENT, f"segment {index} has end_ms <= start_ms"
            )
        if not (0.0 <= segment.confidence <= 1.0):
            raise ProcessingError(
                ErrorCode.INVALID_DIARIZATION_SEGMENT, f"segment {index} confidence out of [0, 1]"
            )
        label = segment.speaker_label.strip()
        if not label:
            raise ProcessingError(
                ErrorCode.INVALID_DIARIZATION_SEGMENT, f"segment {index} has an empty speaker_label"
            )
        if len(label) > MAX_SPEAKER_LABEL_LENGTH:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                f"segment {index} speaker_label exceeds {MAX_SPEAKER_LABEL_LENGTH} characters",
            )
        if not segment.source_segment_id.strip():
            raise ProcessingError(
                ErrorCode.INVALID_DIARIZATION_SEGMENT,
                f"segment {index} has an empty source_segment_id",
            )

    return segments


def diarization_segments_to_mentions(
    segments: tuple[DiarizationSegmentInput, ...],
) -> list[RawMention]:
    """Validate then convert diarization segments into `diarization_speaker_turn` mentions.

    `speaker_label` becomes an `ExtractedEntityMention` with
    `entity_type_hint="speaker_label_local"` — a raw, unresolved,
    source-local label, exactly like a phone number or FIR-mentioned name
    elsewhere in this project; never a resolved identity.
    """
    validated = validate_diarization_segments(segments)
    mentions: list[RawMention] = []
    for index, segment in enumerate(validated):
        locator = SourceLocator(
            time_start_ms=segment.start_ms,
            time_end_ms=segment.end_ms,
            json_path=f"$.segments[{index}]",
        )
        mentions.append(
            RawMention(
                observation_type=OBSERVATION_TYPE,
                text=segment.speaker_label.strip(),
                locator=locator,
                confidence=segment.confidence,
                entity_type_hint=ENTITY_TYPE_HINT,
                attributes={"source_segment_id": segment.source_segment_id},
            )
        )
    return mentions
