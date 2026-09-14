"""Small, deterministic validation results for source-derived event signals.

These results are deliberately producer-local.  They explain whether one
source record is usable as a correlation *input* without creating a candidate,
score, entity, graph relationship, or review decision.  A rejected result is
raised as a safe ``ProcessingError`` by the caller and is never published as a
canonical observation; an accepted result is represented by bounded metadata
on the observation plus its existing evidence/source-locator/extractor fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.modules.structured_processing.models import ParserProfile
from app.modules.structured_processing.provenance import profile_config_hash


class SignalValidationOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class SignalValidationResult:
    """Safe validation outcome for one locatable source record.

    ``source_locator`` and the profile identity make the result reproducible;
    case/evidence/observation IDs remain on the canonical observation itself,
    avoiding duplicate or mutable identity fields in this internal result.
    ``reason_codes`` and ``explanation`` must never contain raw field values.
    """

    outcome: SignalValidationOutcome
    reason_codes: tuple[str, ...]
    explanation: str
    source_locator: SourceLocator
    extractor_name: str
    extractor_version: str
    extractor_config_hash: str

    @classmethod
    def accepted(
        cls, *, profile: ParserProfile, source_locator: SourceLocator
    ) -> SignalValidationResult:
        return cls(
            outcome=SignalValidationOutcome.ACCEPTED,
            reason_codes=(),
            explanation="required source-backed event fields are valid",
            source_locator=source_locator,
            extractor_name=profile.name,
            extractor_version=profile.version,
            extractor_config_hash=profile_config_hash(profile),
        )

    @classmethod
    def rejected(
        cls,
        *,
        profile: ParserProfile,
        source_locator: SourceLocator,
        reason_codes: tuple[str, ...],
        explanation: str,
    ) -> SignalValidationResult:
        return cls(
            outcome=SignalValidationOutcome.REJECTED,
            reason_codes=reason_codes,
            explanation=explanation,
            source_locator=source_locator,
            extractor_name=profile.name,
            extractor_version=profile.version,
            extractor_config_hash=profile_config_hash(profile),
        )

    @classmethod
    def incomplete(
        cls,
        *,
        profile: ParserProfile,
        source_locator: SourceLocator,
        reason_codes: tuple[str, ...],
        explanation: str,
    ) -> SignalValidationResult:
        return cls(
            outcome=SignalValidationOutcome.INCOMPLETE,
            reason_codes=reason_codes,
            explanation=explanation,
            source_locator=source_locator,
            extractor_name=profile.name,
            extractor_version=profile.version,
            extractor_config_hash=profile_config_hash(profile),
        )

    def attribute_value(self) -> dict[str, JsonValue]:
        """The safe, graph-facing subset; locator and case/evidence remain canonical fields."""
        return {
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "explanation": self.explanation,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_config_hash": self.extractor_config_hash,
        }


__all__ = ["SignalValidationOutcome", "SignalValidationResult"]
