"""Deterministic, safe validation outcomes for communication source signals.

The worker keeps raw transcript and message content in the approved evidence
path.  This module deliberately returns only positional/provenance metadata
and machine-readable reason codes, so it is safe to attach its result to a
canonical observation and to expose it to Phase 5's sourcing boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.modules.communication_processing.aliases.normalize import comparison_key, normalize_unicode

if TYPE_CHECKING:
    from app.modules.communication_processing.social.common import ChatMessageRecord


class CommunicationSignalOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCOMPLETE = "incomplete"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class AudioChunkScope:
    """A supplied source-relative audio range; it is not a persistence claim."""

    start_ms: int
    end_ms: int
    manifest_id: str | None = None
    chunk_id: str | None = None


@dataclass(frozen=True)
class CommunicationSignalValidationResult:
    outcome: CommunicationSignalOutcome
    reason_codes: tuple[str, ...]
    reviewer_explanation: str
    source_locator_reference: str
    correlation_ready: bool
    extractor_identity: dict[str, JsonValue]

    def attribute_value(self) -> dict[str, JsonValue]:
        """Return graph-safe metadata only; never source text or secret input."""
        return {
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "reviewer_explanation": self.reviewer_explanation,
            "source_locator_reference": self.source_locator_reference,
            "correlation_ready": self.correlation_ready,
            "extractor_identity": self.extractor_identity,
        }


def safe_locator_reference(locator: SourceLocator) -> str:
    """Produce a bounded positional reference without source content."""
    fields = (
        ("message", locator.message_id),
        ("json", locator.json_path),
        (
            "span",
            f"{locator.span_start}:{locator.span_end}" if locator.span_start is not None else None,
        ),
        (
            "time",
            f"{locator.time_start_ms}:{locator.time_end_ms}"
            if locator.time_start_ms is not None
            else None,
        ),
    )
    return ",".join(f"{name}={value}" for name, value in fields if value) or "source_locator"


def _result(
    outcome: CommunicationSignalOutcome,
    reason_codes: tuple[str, ...],
    explanation: str,
    locator: SourceLocator,
    *,
    extractor_identity: Mapping[str, JsonValue] | None = None,
) -> CommunicationSignalValidationResult:
    return CommunicationSignalValidationResult(
        outcome=outcome,
        reason_codes=reason_codes,
        reviewer_explanation=explanation,
        source_locator_reference=safe_locator_reference(locator),
        correlation_ready=outcome is CommunicationSignalOutcome.ACCEPTED,
        extractor_identity=dict(extractor_identity or {}),
    )


def validate_audio_signal(
    locator: SourceLocator,
    *,
    signal_kind: str,
    chunk_scope: AudioChunkScope | None = None,
    extractor_identity: Mapping[str, JsonValue] | None = None,
) -> CommunicationSignalValidationResult:
    """Validate a source-relative transcript or diarization time range.

    A chunk is checked only when a caller actually supplies its bounds.
    This avoids inventing persisted media scope for transcript imports.
    """
    start, end = locator.time_start_ms, locator.time_end_ms
    if start is None or end is None:
        return _result(
            CommunicationSignalOutcome.INCOMPLETE,
            ("audio_time_missing",),
            "Source-relative audio timing is unavailable.",
            locator,
            extractor_identity=extractor_identity,
        )
    if start < 0 or end < start:
        return _result(
            CommunicationSignalOutcome.REJECTED,
            ("audio_time_invalid",),
            "Source-relative audio timing is invalid.",
            locator,
            extractor_identity=extractor_identity,
        )
    if chunk_scope is not None and (
        chunk_scope.start_ms < 0
        or chunk_scope.end_ms < chunk_scope.start_ms
        or start < chunk_scope.start_ms
        or end > chunk_scope.end_ms
    ):
        return _result(
            CommunicationSignalOutcome.REJECTED,
            ("audio_time_outside_chunk",),
            "The signal falls outside the supplied audio chunk.",
            locator,
            extractor_identity=extractor_identity,
        )
    return _result(
        CommunicationSignalOutcome.ACCEPTED,
        (f"{signal_kind}_timing_valid",),
        "Source-relative timing and supplied provenance are valid.",
        locator,
        extractor_identity=extractor_identity,
    )


def validate_chat_signal(record: ChatMessageRecord) -> CommunicationSignalValidationResult:
    """Validate a parsed social record without returning message text or handles."""
    platform = record.platform.strip().casefold() if isinstance(record.platform, str) else ""
    if not platform:
        return _result(
            CommunicationSignalOutcome.REJECTED,
            ("platform_missing",),
            "Platform is missing.",
            record.locator,
        )
    if not (record.message_id or record.locator.json_path or record.locator.span_start is not None):
        return _result(
            CommunicationSignalOutcome.REJECTED,
            ("message_locator_missing",),
            "Message provenance is missing.",
            record.locator,
        )
    if record.sender is None or not record.sender.strip():
        return _result(
            CommunicationSignalOutcome.INCOMPLETE,
            ("sender_missing",),
            "Sender is unavailable; the record is not identity-ready.",
            record.locator,
        )
    if record.timestamp_raw is None or record.timestamp_utc is None:
        return _result(
            CommunicationSignalOutcome.INCOMPLETE,
            ("timestamp_unresolved",),
            "Message time is unavailable or ambiguous.",
            record.locator,
        )
    return _result(
        CommunicationSignalOutcome.ACCEPTED,
        ("chat_metadata_valid",),
        "Message metadata and locator are valid.",
        record.locator,
    )


def validate_identifier_signal(
    value: str,
    *,
    identifier_type: str,
    platform: str | None,
    locator: SourceLocator,
) -> tuple[CommunicationSignalValidationResult, str]:
    """Return a stable, conservative normalized identifier representation.

    This does not decide that two values identify the same person.  Handles
    retain their platform namespace and aliases/transliterations are explicitly
    candidate-only by consumers.
    """
    normalized = comparison_key(normalize_unicode(value))
    if not normalized:
        return (
            _result(
                CommunicationSignalOutcome.REJECTED,
                ("identifier_empty",),
                "Identifier is empty.",
                locator,
            ),
            "",
        )
    if identifier_type == "handle" and not (platform and platform.strip()):
        return (
            _result(
                CommunicationSignalOutcome.INCOMPLETE,
                ("handle_platform_missing",),
                "Handle namespace is unavailable.",
                locator,
            ),
            normalized,
        )
    if identifier_type not in {
        "handle",
        "phone_like",
        "alias",
        "display_label",
        "speaker_label_local",
    }:
        return (
            _result(
                CommunicationSignalOutcome.REJECTED,
                ("identifier_type_unknown",),
                "Identifier type is unsupported.",
                locator,
            ),
            normalized,
        )
    return (
        _result(
            CommunicationSignalOutcome.ACCEPTED,
            ("identifier_normalized",),
            "Identifier normalization is deterministic.",
            locator,
        ),
        normalized,
    )


def finite_confidence(value: float) -> bool:
    """Keep NaN/infinite model values out of accepted communication signals."""
    return isfinite(value) and 0.0 <= value <= 1.0
