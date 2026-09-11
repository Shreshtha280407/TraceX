"""Typed import of externally-produced transcript segments.

This module never runs ASR (see CLAUDE.md and the explicit non-goals in
`docs/architecture/audio-social-and-communication-processing-v1.md`). It
only validates and normalizes segments a caller already has — from an
approved external transcript-generation process entirely out of scope for
this phase — into deterministic `ObservationV1`-ready `RawMention`s. Text,
timestamps, and confidence are never invented, only validated and passed
through unchanged.

`parse_transcript_import_payload` (Phase 2) is the on-the-wire counterpart
to the in-process `TranscriptImportInput` the rest of this file already
handled: the documented `transcript_import_v1` JSON interchange shape a
`worker.py --once` run resolves from a claimed job's evidence bytes (see
`docs/architecture/communication-processing-worker.md`). It performs only
safe decoding and field-presence/type validation — timing/bounds
validation remains `validate_transcript_segments`'s job, called exactly
once, unchanged, from `transcript_segments_to_mentions`.
"""

from __future__ import annotations

import json

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_TRANSCRIPT_SEGMENT_DURATION_MS,
    MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH,
    MAX_TRANSCRIPT_SEGMENTS,
)
from app.modules.communication_processing.models import (
    RawMention,
    TranscriptImportInput,
    TranscriptSegmentInput,
)

OBSERVATION_TYPE = "transcript_segment"

#: The documented `transcript_import_v1` JSON interchange shape:
#: `{"segments": [{"start_ms": int, "end_ms": int, "text": str,
#: "language_hint": str | null, "confidence": float,
#: "source_segment_id": str}, ...]}`. Every field is required except
#: `language_hint`.
_REQUIRED_SEGMENT_FIELDS = ("start_ms", "end_ms", "text", "confidence", "source_segment_id")


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


def parse_transcript_import_payload(data: bytes) -> TranscriptImportInput:
    """Deserialize the `transcript_import_v1` JSON interchange payload.

    Safe UTF-8 decode + JSON parse + field-presence/type validation only —
    never timing/bounds validation (that's `validate_transcript_segments`'s
    job, run later by `transcript_segments_to_mentions`). Raises
    `malformed_json_payload` for anything not matching the documented
    shape, mirroring `social/telegram.py::parse_telegram_export`'s exact
    style for parsing untrusted JSON bytes.
    """
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(
            ErrorCode.MALFORMED_JSON_PAYLOAD, "payload is not valid UTF-8 JSON"
        ) from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("segments"), list):
        raise ProcessingError(
            ErrorCode.MALFORMED_JSON_PAYLOAD,
            "expected a top-level object with a 'segments' array",
        )

    raw_segments: list[object] = parsed["segments"]
    if len(raw_segments) > MAX_TRANSCRIPT_SEGMENTS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"payload exceeds the {MAX_TRANSCRIPT_SEGMENTS}-segment limit",
        )

    segments = tuple(_segment_from_json(entry, index) for index, entry in enumerate(raw_segments))
    return TranscriptImportInput(segments=segments)


def _segment_from_json(entry: object, index: int) -> TranscriptSegmentInput:
    if not isinstance(entry, dict):
        raise ProcessingError(ErrorCode.MALFORMED_JSON_PAYLOAD, f"segment {index} is not an object")
    missing = [field for field in _REQUIRED_SEGMENT_FIELDS if field not in entry]
    if missing:
        raise ProcessingError(
            ErrorCode.MALFORMED_JSON_PAYLOAD, f"segment {index} is missing required field(s)"
        )

    start_ms, end_ms = entry["start_ms"], entry["end_ms"]
    text, confidence = entry["text"], entry["confidence"]
    source_segment_id = entry["source_segment_id"]
    language_hint = entry.get("language_hint")

    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(text, str)
        or not isinstance(confidence, int | float)
        or isinstance(confidence, bool)
        or not isinstance(source_segment_id, str)
        or (language_hint is not None and not isinstance(language_hint, str))
    ):
        raise ProcessingError(
            ErrorCode.MALFORMED_JSON_PAYLOAD, f"segment {index} has a field of the wrong type"
        )

    return TranscriptSegmentInput(
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        language_hint=language_hint,
        confidence=float(confidence),
        source_segment_id=source_segment_id,
    )
