"""ObservationV1: a single provenance-rich fact extracted from evidence.

Observations are the canonical output of every source extractor/worker.
They sit strictly between raw evidence and the graph: `extracted_entities`
here are raw mentions (a name string, a phone number as written), not
resolved `EntityV1` references — entity resolution is later-phase work, and
an `EntityV1` instead points back to the observations it was created from.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue

from app.contracts.common import (
    ContractVersion,
    Extractor,
    Location,
    SourceLocator,
    TimeWindow,
    TraceXModel,
)


class ExtractedEntityMention(TraceXModel):
    """A raw, unresolved entity mention as it literally appeared in the source."""

    text: str = Field(min_length=1)
    entity_type_hint: str | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class ObservationV1(TraceXModel):
    """A single extracted, source-locatable fact.

    `extraction_confidence` is extraction/signal quality only — never a
    guilt probability. `source_locator` is mandatory: an observation that
    cannot point back to where in the evidence it came from is not usable
    as evidence-first intelligence.
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    observation_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_type: str = Field(min_length=1)
    extracted_entities: list[ExtractedEntityMention] = Field(default_factory=list)
    event_time: AwareDatetime | None = None
    time_window: TimeWindow | None = None
    location: Location | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    source_locator: SourceLocator
    extractor: Extractor
    created_at: AwareDatetime
