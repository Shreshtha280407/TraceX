"""EventV1: a time-bounded occurrence linking two or more entities.

Events (calls, transactions, sightings, messages, meetings) are the only
way two entities connect in TraceX. Timeless direct entity-to-entity edges
are deliberately not modeled anywhere in these contracts — every connection
must be able to answer "when did this happen", which is what makes the
graph temporal and auditable rather than a bag of unexplained associations.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.contracts.common import ContractVersion, Location, ReviewStatus, TimeWindow, TraceXModel


class EventV1(TraceXModel):
    """A time-bounded occurrence connecting one or more entities.

    `confidence` is statement quality (how well-supported is the claim that
    this event happened as described) — never a guilt probability.
    `evidence_refs` must be non-empty: every event must cite the evidence
    it was derived from.
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    event_id: UUID
    case_id: UUID
    event_type: str = Field(min_length=1)
    participant_entity_ids: list[UUID] = Field(min_length=1)
    event_time: AwareDatetime | None = None
    time_window: TimeWindow | None = None
    location: Location | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[UUID] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_time_bounded(self) -> EventV1:
        if self.event_time is None and self.time_window is None:
            raise ValueError("EventV1 requires event_time or time_window: events are time-bounded")
        return self
