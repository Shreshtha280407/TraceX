"""Deterministic producer-side validation for correlation-ready visual signals.

This boundary is deliberately local to media processing.  It validates
source-relative locator/timeline/geometry provenance and produces bounded
quality metadata; it never creates candidates, identities, graph writes, or
review decisions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue

from app.contracts.common import Extractor, SourceLocator
from app.modules.evidence_lifecycle.media_orchestration import ChunkBoundary
from app.modules.media_processing.models import VideoMetadata


class VisualSignalOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class VisualSignalValidationResult:
    """Safe, deterministic source-quality result attached to a visual observation."""

    outcome: VisualSignalOutcome
    reason_codes: tuple[str, ...]
    explanation: str
    source_locator: SourceLocator
    extractor_name: str
    extractor_version: str
    extractor_config_hash: str

    @property
    def correlation_ready(self) -> bool:
        return self.outcome is VisualSignalOutcome.ACCEPTED

    def attribute_value(self) -> dict[str, JsonValue]:
        return {
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "explanation": self.explanation,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_config_hash": self.extractor_config_hash,
            "correlation_ready": self.correlation_ready,
        }


def chunk_for_source_time(
    *, boundaries: tuple[ChunkBoundary, ...], time_start_ms: int, time_end_ms: int
) -> ChunkBoundary | None:
    """Return the sole planned chunk containing a source-relative interval.

    A signal spanning chunks is deliberately not assigned arbitrarily: callers
    receive ``None`` and validation marks it rejected when a chunk is required.
    """
    matches = [
        boundary
        for boundary in boundaries
        if boundary.time_start_ms is not None
        and boundary.time_end_ms is not None
        and boundary.time_start_ms <= time_start_ms <= time_end_ms <= boundary.time_end_ms
    ]
    return matches[0] if len(matches) == 1 else None


def frame_number_for_time(metadata: VideoMetadata, time_ms: int) -> int | None:
    """Return the only deterministic CFR mapping this module is allowed to claim."""
    if time_ms < 0 or metadata.frame_rate is None or metadata.frame_count is None:
        return None
    if not math.isfinite(metadata.frame_rate) or metadata.frame_rate <= 0:
        return None
    return max(0, min(int(round(time_ms * metadata.frame_rate / 1000)), metadata.frame_count - 1))


def validate_visual_signal(
    *,
    locator: SourceLocator,
    extractor: Extractor,
    video_metadata: VideoMetadata | None = None,
    chunk_boundary: ChunkBoundary | None = None,
    require_bbox: bool = False,
) -> VisualSignalValidationResult:
    """Validate a locatable image/video signal without changing its geometry.

    Unknown frame mapping is an *incomplete* result: the signal is preserved
    for review but explicitly cannot be promoted as correlation-ready input.
    """
    reasons: list[str] = []
    outcome = VisualSignalOutcome.ACCEPTED
    if require_bbox and locator.bbox_xyxy_normalized is None:
        reasons.append("missing_normalized_geometry")
    if locator.bbox_xyxy_normalized is not None:
        box = locator.bbox_xyxy_normalized
        if not all(math.isfinite(value) for value in (box.x_min, box.y_min, box.x_max, box.y_max)):
            reasons.append("non_finite_normalized_geometry")

    if video_metadata is not None:
        start, end = locator.time_start_ms, locator.time_end_ms
        if start is None or end is None:
            outcome = VisualSignalOutcome.INCOMPLETE
            reasons.append("missing_source_relative_time_bounds")
        elif end > video_metadata.duration_ms:
            reasons.append("time_outside_media_duration")
        if start is not None and end is not None and chunk_boundary is not None:
            chunk_start, chunk_end = chunk_boundary.time_start_ms, chunk_boundary.time_end_ms
            if (chunk_start is not None and start < chunk_start) or (
                chunk_end is not None and end > chunk_end
            ):
                reasons.append("time_outside_processed_chunk")
        if locator.frame_number is None:
            outcome = VisualSignalOutcome.INCOMPLETE
            reasons.append("frame_mapping_unavailable")
        elif start is not None:
            expected = frame_number_for_time(video_metadata, start)
            if expected is None:
                outcome = VisualSignalOutcome.INCOMPLETE
                reasons.append("frame_mapping_unavailable")
            elif locator.frame_number != expected:
                reasons.append("frame_time_mapping_mismatch")
        if (
            chunk_boundary is not None
            and locator.frame_number is not None
            and (
                (
                    chunk_boundary.frame_start is not None
                    and locator.frame_number < chunk_boundary.frame_start
                )
                or (
                    chunk_boundary.frame_end is not None
                    and locator.frame_number > chunk_boundary.frame_end
                )
            )
        ):
            reasons.append("frame_outside_processed_chunk")

    if any(
        code in reasons
        for code in (
            "missing_normalized_geometry",
            "non_finite_normalized_geometry",
            "time_outside_media_duration",
            "time_outside_processed_chunk",
            "frame_time_mapping_mismatch",
            "frame_outside_processed_chunk",
        )
    ):
        outcome = VisualSignalOutcome.REJECTED
    explanation = {
        VisualSignalOutcome.ACCEPTED: "visual source locator, geometry, and provenance are valid",
        VisualSignalOutcome.REJECTED: (
            "visual source signal failed deterministic provenance validation"
        ),
        VisualSignalOutcome.INCOMPLETE: (
            "visual source signal lacks precision required for correlation input"
        ),
    }[outcome]
    return VisualSignalValidationResult(
        outcome=outcome,
        reason_codes=tuple(sorted(set(reasons))),
        explanation=explanation,
        source_locator=locator,
        extractor_name=extractor.name,
        extractor_version=extractor.version,
        extractor_config_hash=extractor.config_hash,
    )


__all__ = [
    "VisualSignalOutcome",
    "VisualSignalValidationResult",
    "frame_number_for_time",
    "validate_visual_signal",
]
