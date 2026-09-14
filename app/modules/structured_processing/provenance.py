"""Deterministic observation IDs and extractor provenance.

Reuses the project's existing deterministic-ID and canonical-serialization
helpers (`app.core.ids`, `app.core.canonical`) rather than reimplementing
hashing here — see `docs/decisions/ADR-002-deterministic-source-processing-
and-provenance.md` for why the same source location must always yield the
same observation ID.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.core.canonical import canonical_bytes, canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.structured_processing.models import ParserProfile, RawMention

# Deterministic confidence levels. `extraction_confidence` is extraction/
# statement quality only — never a probability of guilt. No value here is
# ever produced by a model; every one is a fixed constant tied to a
# documented rule (see docs/architecture/document-and-structured-processing-v1.md).
CONFIDENCE_STRUCTURED_COMPLETE = 1.00  # a complete record read directly from a validated row/object
CONFIDENCE_REGEX_EXACT_MATCH = (
    0.95  # an exact deterministic regex match in trustworthy embedded text
)
# a structured field read directly, but normalization was withheld as uncertain
# (original value retained)
CONFIDENCE_NORMALIZATION_CONSERVATIVE = 0.90
# a regex match with weaker surrounding context (e.g. an amount with no explicit currency symbol)
CONFIDENCE_REGEX_WEAK_CONTEXT = 0.80


def profile_config_hash(profile: ParserProfile) -> str:
    """A stable hash identifying this profile's defining configuration.

    Changes whenever the profile's observation types or field aliases
    change, so `Extractor.config_hash` genuinely reflects "which extraction
    logic produced this" rather than being a constant placeholder.
    """
    return canonical_sha256(
        {
            "name": profile.name,
            "version": profile.version,
            "observation_types": sorted(profile.observation_types),
            "field_aliases": {k: sorted(v) for k, v in profile.field_aliases.items()},
            "required_fields": sorted(profile.required_fields),
        }
    )


def build_extractor(profile: ParserProfile) -> Extractor:
    """The `Extractor` provenance block for every observation this profile emits.

    `model_version="n/a"`: this module uses no ML model, per project policy
    (see `app.contracts.common.Extractor` and CLAUDE.md).
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
    profile: ParserProfile,
    observation_type: str,
    locator: SourceLocator,
    discriminator: str = "",
) -> UUID:
    """A deterministic observation ID.

    Stable input: case_id, evidence_id, parser profile name+version,
    observation_type, and the exact source locator (every locator field,
    via canonical serialization). Reprocessing the same evidence with the
    same profile version always yields the same ID for the same source
    location; a changed locator, profile version, or observation type
    always yields a different one.
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
    profile: ParserProfile,
    mention: RawMention,
    created_at: datetime,
    discriminator: str = "",
) -> ObservationV1:
    """Build one `ObservationV1` from a `RawMention`.

    `mention.text` becomes an `ExtractedEntityMention` only when
    `entity_type_hint` is set — record-level observations (a whole CDR row,
    a whole financial transaction) describe themselves through
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
        time_window=mention.time_window,
        attributes=mention.attributes,
        extraction_confidence=mention.confidence,
        source_locator=mention.locator,
        extractor=build_extractor(profile),
        created_at=created_at,
    )
