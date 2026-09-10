"""EntityV1: a graph-level entity resolved from one or more observations.

`entity_type` is deliberately a plain string, not an enum: the detailed
entity/relationship taxonomy is owned by a later phase (Shreshtha). Locking
it to an enum now would force every contributor to edit this frozen
contract just to add a taxonomy value. This module defines the envelope
only — entity resolution/merging logic is later-phase work.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue

from app.contracts.common import ContractVersion, ReviewStatus, TraceXModel


class EntityV1(TraceXModel):
    """A resolved entity (person, organization, phone number, account, ...).

    `created_from_observation_ids` must be non-empty: an entity that traces
    back to zero observations violates the evidence-first principle — it
    would be an analyst assertion with no source.
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    entity_id: UUID
    case_id: UUID
    entity_type: str = Field(min_length=1)
    canonical_label: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    stable_identifiers: dict[str, JsonValue] = Field(default_factory=dict)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    created_from_observation_ids: list[UUID] = Field(min_length=1)
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED
    created_at: AwareDatetime
