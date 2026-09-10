"""Shared primitives used by every versioned TraceX contract.

These types encode the cross-cutting rules from the Phase 1 contract spec:
confidence is extraction/statement quality (never guilt probability), every
observation must be locatable back to its source, and time bounds must be
internally consistent. Validation lives here once so every contract that
embeds these types inherits it, instead of being re-checked per contract.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ContractVersion(StrEnum):
    """Supported schema versions for TraceX shared contracts."""

    V1 = "v1"


class ReviewStatus(StrEnum):
    """Human-review state of a graph-level object (entity or event).

    Distinct from `extraction_confidence` / `confidence`: review status is
    an analyst decision, confidence is a statement about extraction/signal
    quality. Neither is a guilt determination.
    """

    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    REJECTED = "rejected"


class TraceXModel(BaseModel):
    """Base class for all TraceX shared contracts.

    `extra="forbid"` deliberately rejects unknown fields: these contracts
    are the frozen interface between independently-built modules, so a
    silently-dropped typo'd field is worse than a loud validation error.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BoundingBoxNormalized(TraceXModel):
    """Axis-aligned box in normalized `[0, 1]` image/frame coordinates."""

    x_min: float = Field(ge=0.0, le=1.0)
    y_min: float = Field(ge=0.0, le=1.0)
    x_max: float = Field(ge=0.0, le=1.0)
    y_max: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_geometry(self) -> BoundingBoxNormalized:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("bbox_xyxy_normalized requires x_min < x_max and y_min < y_max")
        return self


class SourceLocator(TraceXModel):
    """Pointer from an observation back to the exact spot in the source.

    Every field is optional individually because locator shape depends on
    modality (a PDF uses `page`/`span_*`, a spreadsheet uses `sheet`/`row`/
    `column`, a video uses `frame_number`/`time_*_ms`), but at least one
    field must be set: an observation with no locator at all cannot be
    traced back to evidence, which violates the evidence-first principle.
    """

    page: int | None = Field(default=None, ge=1)
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    bbox_xyxy_normalized: BoundingBoxNormalized | None = None
    sheet: str | None = None
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    json_path: str | None = None
    frame_number: int | None = Field(default=None, ge=0)
    time_start_ms: int | None = Field(default=None, ge=0)
    time_end_ms: int | None = Field(default=None, ge=0)
    message_id: str | None = None

    @model_validator(mode="after")
    def _validate_locator(self) -> SourceLocator:
        values = (
            self.page,
            self.span_start,
            self.span_end,
            self.bbox_xyxy_normalized,
            self.sheet,
            self.row,
            self.column,
            self.json_path,
            self.frame_number,
            self.time_start_ms,
            self.time_end_ms,
            self.message_id,
        )
        if all(value is None for value in values):
            raise ValueError("source_locator requires at least one modality-relevant field")
        if (
            self.time_start_ms is not None
            and self.time_end_ms is not None
            and self.time_end_ms < self.time_start_ms
        ):
            raise ValueError("source_locator.time_end_ms must be >= time_start_ms")
        if (
            self.span_start is not None
            and self.span_end is not None
            and self.span_end < self.span_start
        ):
            raise ValueError("source_locator.span_end must be >= span_start")
        return self


class TimeWindow(TraceXModel):
    """An optionally-bounded time range, used where a single instant is too precise."""

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> TimeWindow:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("time_window.end must be >= time_window.start")
        return self


class Location(TraceXModel):
    """A loosely-structured place reference; precision varies by source quality."""

    raw_text: str | None = None
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    precision_meters: float | None = Field(default=None, ge=0.0)


class Extractor(TraceXModel):
    """Identifies exactly which code/config/model version produced an observation.

    `model_version` is required even for non-ML extractors (use a constant
    such as `"n/a"`) so the field is never ambiguous between "no model" and
    "unknown model" — see docs/architecture/contracts.md.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
