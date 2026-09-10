"""Typed import of externally-produced transcript segments.

This module never runs ASR (see CLAUDE.md and the explicit non-goals in
`docs/architecture/audio-social-and-communication-processing-v1.md`). It
only validates and normalizes segments a caller already has — from an
approved external transcript-generation process entirely out of scope for
this phase — into deterministic `ObservationV1`-ready `RawMention`s. Text,
timestamps, and confidence are never invented, only validated and passed
through unchanged.
"""

from __future__ import annotations

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_TRANSCRIPT_SEGMENT_DURATION_MS,
    MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH,
    MAX_TRANSCRIPT_SEGMENTS,
)
from app.modules.communication_processing.models import RawMention, TranscriptSegmentInput

OBSERVATION_TYPE = "transcript_segment"


def _normalize_language_hint(raw: str | None) -> str | None:
    """Safe normalization only: strip and lowercase. Never translates or guesses."""
    if raw is None:
        return None
    normalized = raw.strip().lower()
    return normalized or None


def validate_transcript_segments(
    segments: tuple[TranscriptSegmentInput, ...],
) -> tuple[TranscriptSegmentInput, ...]:
    """Validate segment bounds; raise on the first violation.

    Rules: `start_ms >= 0`; `end_ms > start_ms`; segment duration bounded
    by `MAX_TRANSCRIPT_SEGMENT_DURATION_MS`; text length bounded by
    `MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH`; confidence in `[0.0, 1.0]`;
    non-empty `source_segment_id`. `language_hint` is preserved after a
    safe normalization (stripped, lowercased) — never translated, and
    never guessed when absent.
    """
    if len(segments) > MAX_TRANSCRIPT_SEGMENTS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"transcript exceeds the {MAX_TRANSCRIPT_SEGMENTS}-segment limit",
        )

    normalized: list[TranscriptSegmentInput] = []
    for index, segment in enumerate(segments):
        if segment.start_ms < 0:
            raise ProcessingError(
                ErrorCode.INVALID_TRANSCRIPT_SEGMENT, f"segment {index} has start_ms < 0"
            )
        if segment.end_ms <= segment.start_ms:
            raise ProcessingError(
                ErrorCode.INVALID_TRANSCRIPT_SEGMENT, f"segment {index} has end_ms <= start_ms"
            )
        if (segment.end_ms - segment.start_ms) > MAX_TRANSCRIPT_SEGMENT_DURATION_MS:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                f"segment {index} exceeds the "
                f"{MAX_TRANSCRIPT_SEGMENT_DURATION_MS}-ms duration limit",
            )
        if not (0.0 <= segment.confidence <= 1.0):
            raise ProcessingError(
                ErrorCode.INVALID_TRANSCRIPT_SEGMENT, f"segment {index} confidence out of [0, 1]"
            )
        if len(segment.text) > MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                f"segment {index} text exceeds the "
                f"{MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH}-character limit",
            )
        if not segment.source_segment_id.strip():
            raise ProcessingError(
                ErrorCode.INVALID_TRANSCRIPT_SEGMENT,
                f"segment {index} has an empty source_segment_id",
            )

        normalized.append(
            TranscriptSegmentInput(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                language_hint=_normalize_language_hint(segment.language_hint),
                confidence=segment.confidence,
                source_segment_id=segment.source_segment_id,
            )
        )
    return tuple(normalized)


def transcript_segments_to_mentions(
    segments: tuple[TranscriptSegmentInput, ...],
) -> list[RawMention]:
    """Validate then convert transcript segments into `transcript_segment` mentions.

    `extraction_confidence` on the resulting observation is the caller-
    supplied `segment.confidence` unchanged — transcript quality only,
    never truth or identity confidence.
    """
    validated = validate_transcript_segments(segments)
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
                text=f"transcript_segment[{index}]",
                locator=locator,
                confidence=segment.confidence,
                entity_type_hint=None,
                attributes={
                    "text": segment.text,
                    "source_segment_id": segment.source_segment_id,
                    "language_hint": segment.language_hint,
                },
            )
        )
    return mentions
