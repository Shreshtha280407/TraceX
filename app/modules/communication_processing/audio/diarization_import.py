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

`parse_diarization_import_payload` (Phase 2) is the on-the-wire
counterpart to the in-process `DiarizationImportInput` this file already
handled: the documented `diarization_import_v1` JSON interchange shape a
`worker.py --once` run resolves from a claimed job's evidence bytes (see
`docs/architecture/communication-processing-worker.md`). It performs only
safe decoding and field-presence/type validation — timing/bounds
validation remains `validate_diarization_segments`'s job, unchanged.
"""

from __future__ import annotations

import json

from app.contracts.common import SourceLocator
from app.modules.communication_processing.audio.diarization_adapter import (
    SEGMENT_SOURCE_METADATA_SUPPLIED,
)
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_DIARIZATION_SEGMENTS,
    MAX_SPEAKER_LABEL_LENGTH,
)
from app.modules.communication_processing.models import (
    DiarizationImportInput,
    DiarizationSegmentInput,
    RawMention,
)

OBSERVATION_TYPE = "diarization_speaker_turn"
ENTITY_TYPE_HINT = "speaker_label_local"

#: The documented `diarization_import_v1` JSON interchange shape:
#: `{"segments": [{"start_ms": int, "end_ms": int, "speaker_label": str,
#: "confidence": float, "source_segment_id": str}, ...]}`. Every field is
#: required.
_REQUIRED_SEGMENT_FIELDS = (
    "start_ms",
    "end_ms",
    "speaker_label",
    "confidence",
    "source_segment_id",
)


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

    `segment_source` is always `SEGMENT_SOURCE_METADATA_SUPPLIED`: this
    function only ever imports externally-produced segments (see this
    module's docstring) -- no `DiarizationAdapter` in this phase ever
    reports `READY` (see `audio/diarization_adapter.py`), so no segment is
    ever `SEGMENT_SOURCE_MODEL_DERIVED` today. The attribute exists so a
    future real adapter's output is distinguishable from an imported one
    without an observation-shape change.
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
                attributes={
                    "source_segment_id": segment.source_segment_id,
                    "segment_source": SEGMENT_SOURCE_METADATA_SUPPLIED,
                },
            )
        )
    return mentions


def parse_diarization_import_payload(data: bytes) -> DiarizationImportInput:
    """Deserialize the `diarization_import_v1` JSON interchange payload.

    Safe UTF-8 decode + JSON parse + field-presence/type validation only —
    never timing/bounds validation (that's `validate_diarization_segments`'s
    job, run later by `diarization_segments_to_mentions`). Raises
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
    if len(raw_segments) > MAX_DIARIZATION_SEGMENTS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"payload exceeds the {MAX_DIARIZATION_SEGMENTS}-segment limit",
        )

    segments = tuple(_segment_from_json(entry, index) for index, entry in enumerate(raw_segments))
    return DiarizationImportInput(segments=segments)


def _segment_from_json(entry: object, index: int) -> DiarizationSegmentInput:
    if not isinstance(entry, dict):
        raise ProcessingError(ErrorCode.MALFORMED_JSON_PAYLOAD, f"segment {index} is not an object")
    missing = [field for field in _REQUIRED_SEGMENT_FIELDS if field not in entry]
    if missing:
        raise ProcessingError(
            ErrorCode.MALFORMED_JSON_PAYLOAD, f"segment {index} is missing required field(s)"
        )

    start_ms, end_ms = entry["start_ms"], entry["end_ms"]
    speaker_label, confidence = entry["speaker_label"], entry["confidence"]
    source_segment_id = entry["source_segment_id"]

    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(speaker_label, str)
        or not isinstance(confidence, int | float)
        or isinstance(confidence, bool)
        or not isinstance(source_segment_id, str)
    ):
        raise ProcessingError(
            ErrorCode.MALFORMED_JSON_PAYLOAD, f"segment {index} has a field of the wrong type"
        )

    return DiarizationSegmentInput(
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_label=speaker_label,
        confidence=float(confidence),
        source_segment_id=source_segment_id,
    )
