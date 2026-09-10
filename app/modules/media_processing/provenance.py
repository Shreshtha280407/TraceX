"""Deterministic observation IDs and extractor provenance for media output.

Reuses the project's existing deterministic-ID and canonical-serialization
helpers (`app.core.ids`, `app.core.canonical`), the same way
`app.modules.structured_processing.provenance` does. See
`docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md` for why
the same source location must always yield the same observation ID, and
for the `media_metadata`'s `time_start_ms=0` locator convention.
"""

from __future__ import annotations

import math
from datetime import datetime
from uuid import UUID

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.core.canonical import canonical_bytes, canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.media_processing.models import MediaObservationDraft

# Observation types this module emits. See
# docs/architecture/media-observation-taxonomy-v1.md for the full taxonomy.
OBSERVATION_MEDIA_METADATA = "media_metadata"
OBSERVATION_OBJECT_DETECTION = "object_detection"
OBSERVATION_ANONYMOUS_TRACK_SEGMENT = "anonymous_track_segment"
OBSERVATION_TEXT_REGION_DETECTION = "text_region_detection"
OBSERVATION_OCR_TEXT_MENTION = "ocr_text_mention"

# Deterministic confidence levels. `extraction_confidence` is
# extraction/statement quality only -- never a probability of guilt. See
# docs/architecture/media-processing-v1.md's confidence policy.
CONFIDENCE_METADATA_PROBED = 1.00  # metadata read directly from a successful ffprobe/decode


def is_valid_confidence(value: float) -> bool:
    """True only for a finite value in `[0, 1]`.

    Used to reject malformed detector/tracker/OCR output before an
    observation is ever constructed from it -- see `worker.py` and
    CLAUDE.md: confidence is never invented or clamped into range.
    """
    return math.isfinite(value) and 0.0 <= value <= 1.0


def build_extractor(
    *, processor_name: str, processor_version: str, model_version: str
) -> Extractor:
    """The `Extractor` provenance block for one processor/model combination.

    `config_hash` changes whenever the processor identity or the analysis
    component's declared interface version changes, so it genuinely
    reflects "which code/config produced this."
    """
    config_hash = canonical_sha256(
        {
            "processor_name": processor_name,
            "processor_version": processor_version,
            "model_version": model_version,
        }
    )
    return Extractor(
        name=processor_name,
        version=processor_version,
        config_hash=config_hash,
        model_version=model_version,
    )


def observation_id(
    *,
    case_id: UUID,
    evidence_id: UUID,
    processor_name: str,
    processor_version: str,
    observation_type: str,
    locator: SourceLocator,
    discriminator: str = "",
) -> UUID:
    """A deterministic observation ID.

    Stable input: case_id, evidence_id, processor name+version,
    observation_type, the exact source locator (via canonical
    serialization, which already covers the normalized bounding box when
    present), and an optional discriminator (e.g. a `local_track_id`).
    Reprocessing the same evidence with the same processor version always
    yields the same ID for the same source location.
    """
    return deterministic_uuid(
        str(case_id),
        str(evidence_id),
        processor_name,
        processor_version,
        observation_type,
        canonical_bytes(locator).decode("utf-8"),
        discriminator,
    )


def draft_to_observation(
    *,
    case_id: UUID,
    evidence_id: UUID,
    draft: MediaObservationDraft,
    extractor: Extractor,
    created_at: datetime,
) -> ObservationV1:
    """Build one `ObservationV1` from a `MediaObservationDraft`.

    `draft.entity_text` becomes an `ExtractedEntityMention` only when set
    (used for `ocr_text_mention`) -- detection/tracking/metadata
    observations describe themselves through `attributes` instead, the
    same convention `structured_processing.provenance.mention_to_observation`
    uses for record-level mentions.
    """
    extracted_entities = (
        [ExtractedEntityMention(text=draft.entity_text, entity_type_hint=draft.entity_type_hint)]
        if draft.entity_text is not None
        else []
    )
    return ObservationV1(
        observation_id=observation_id(
            case_id=case_id,
            evidence_id=evidence_id,
            processor_name=extractor.name,
            processor_version=extractor.version,
            observation_type=draft.observation_type,
            locator=draft.locator,
            discriminator=draft.discriminator,
        ),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type=draft.observation_type,
        extracted_entities=extracted_entities,
        attributes=draft.attributes,
        extraction_confidence=draft.confidence,
        source_locator=draft.locator,
        extractor=extractor,
        created_at=created_at,
    )
