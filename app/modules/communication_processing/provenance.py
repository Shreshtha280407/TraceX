"""Deterministic observation IDs and extractor provenance.

Reuses the project's existing deterministic-ID and canonical-serialization
helpers (`app.core.ids`, `app.core.canonical`) — the same pattern as
`app.modules.structured_processing.provenance` — rather than reimplementing
hashing here. See `docs/decisions/ADR-005-provenance-first-communication-
processing.md` for why the same source location must always yield the
same observation ID.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.core.canonical import canonical_bytes, canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.communication_processing.models import ProcessorProfile, RawMention

# Deterministic confidence policy. `extraction_confidence` is extraction/
# statement quality only -- never a probability of guilt, truth, or
# identity. No value here is ever produced by a model.
#
# `CONFIDENCE_STRUCTURED_COMPLETE`: a complete record read directly from a
# validated source (a WAV header fact, a parsed chat message field).
#
# `CONFIDENCE_REGEX_EXACT_MATCH`: an exact deterministic regex match
# against unstructured message text (a phone number, email address, URL,
# or handle -- see `social/identifiers.py`) -- slightly below
# `CONFIDENCE_STRUCTURED_COMPLETE` because the match is a pattern over free
# text rather than a value read from an already-validated structured
# field, mirroring `structured_processing.provenance`'s identical
# constant/rationale.
#
# Transcript-segment and diarization-segment observations do NOT use a
# fixed constant here: `extraction_confidence` is the caller-supplied
# transcript/diarization confidence value itself (validated into [0, 1] by
# `audio/transcript_import.py` / `audio/diarization_import.py`), passed
# through unchanged -- this module never invents or overrides it, since
# doing so would misrepresent how confident the *external* ASR/diarization
# system actually was.
CONFIDENCE_STRUCTURED_COMPLETE = 1.00
CONFIDENCE_REGEX_EXACT_MATCH = 0.95


def profile_config_hash(profile: ProcessorProfile) -> str:
    """A stable hash identifying this profile's defining configuration."""
    return canonical_sha256(
        {
            "name": profile.name,
            "version": profile.version,
            "observation_types": sorted(profile.observation_types),
        }
    )


def build_extractor(profile: ProcessorProfile) -> Extractor:
    """The `Extractor` provenance block for every observation this profile emits.

    `model_version="n/a"`: this module uses no ML model, per project policy
    (see `app.contracts.common.Extractor` and CLAUDE.md). This holds even
    for imported transcript/diarization data: the *import* logic is not a
    model, even though the segments themselves originated from an external
    ASR/diarization system this module never runs.
    """
    return Extractor(
        name=profile.name,
        version=profile.version,
        config_hash=profile_config_hash(profile),
        model_version="n/a",
    )


def observation_id(
    *,
    case_id: UUID,
    evidence_id: UUID,
    profile: ProcessorProfile,
    observation_type: str,
    locator: SourceLocator,
    discriminator: str = "",
) -> UUID:
    """A deterministic observation ID.

    Stable input: case_id, evidence_id, processor profile name+version,
    observation_type, and the exact source locator (every locator field,
    via canonical serialization). Reprocessing identical normalized input
    always yields the same ID for the same source location; a changed
    locator, profile version, or observation type always yields a
    different one. This is this module's documented idempotency behavior
    (see `worker.py`).
    """
    return deterministic_uuid(
        str(case_id),
        str(evidence_id),
        profile.name,
        profile.version,
        observation_type,
        canonical_bytes(locator).decode("utf-8"),
        discriminator,
    )


def mention_to_observation(
    *,
    case_id: UUID,
    evidence_id: UUID,
    profile: ProcessorProfile,
    mention: RawMention,
    created_at: datetime,
    discriminator: str = "",
) -> ObservationV1:
    """Build one `ObservationV1` from a `RawMention`.

    `mention.text` becomes an `ExtractedEntityMention` only when
    `entity_type_hint` is set -- record-level observations (a whole
    transcript segment, a whole chat message) describe themselves through
    `attributes` instead, since there's no single "mention text" for an
    entire record.
    """
    extracted_entities = (
        [ExtractedEntityMention(text=mention.text, entity_type_hint=mention.entity_type_hint)]
        if mention.entity_type_hint is not None
        else []
    )
    return ObservationV1(
        observation_id=observation_id(
            case_id=case_id,
            evidence_id=evidence_id,
            profile=profile,
            observation_type=mention.observation_type,
            locator=mention.locator,
            discriminator=discriminator,
        ),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type=mention.observation_type,
        extracted_entities=extracted_entities,
        event_time=mention.event_time,
        attributes=mention.attributes,
        extraction_confidence=mention.confidence,
        source_locator=mention.locator,
        extractor=build_extractor(profile),
        created_at=created_at,
    )
