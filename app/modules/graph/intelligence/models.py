"""Typed, immutable inputs and outputs for deterministic graph intelligence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, field_validator

from app.modules.graph.models import GraphModel


class RetrievalReason(StrEnum):
    EXACT_IDENTIFIER = "exact_identifier"
    EXACT_ALIAS = "exact_alias"
    NORMALIZED_ALIAS = "normalized_alias"
    TRANSLITERATION = "transliteration_candidate"
    PLATFORM_HANDLE = "platform_scoped_handle"
    VECTOR = "local_vector_candidate"
    TEMPORAL = "temporal_hot_window"


class CandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class ObservationDescriptor(GraphModel):
    """Bounded source-backed fields used by retrieval; never a resolved entity."""

    case_id: UUID
    observation_id: UUID
    evidence_id: UUID
    source_locator_reference: str
    identifiers: dict[str, str] = Field(default_factory=dict)
    identifier_normalization_version: str = "phase5_identifier_normalization_v1"
    aliases: tuple[str, ...] = ()
    transliterations: tuple[str, ...] = ()
    platform: str | None = None
    handles: tuple[str, ...] = ()
    event_start: datetime | None = None
    event_end: datetime | None = None

    @field_validator("source_locator_reference")
    @classmethod
    def require_bounded_locator_reference(cls, value: str) -> str:
        if not value.strip() or len(value) > 512:
            raise ValueError("source locator reference must be non-empty and bounded")
        return value

    @field_validator("identifiers")
    @classmethod
    def require_bounded_identifier_claims(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not key.strip() or not item.strip() or len(item) > 256 for key, item in value.items()
        ):
            raise ValueError("identifier claims must be non-empty and bounded")
        return value


class RetrievedCandidate(GraphModel):
    case_id: UUID
    left_observation_id: UUID
    right_observation_id: UUID
    reasons: tuple[RetrievalReason, ...]
    identifier_types: tuple[str, ...] = ()
    vector_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    supporting_observation_ids: tuple[UUID, ...]
    supporting_evidence_ids: tuple[UUID, ...]
    contradiction_reasons: tuple[str, ...] = ()


class FeatureContribution(GraphModel):
    feature: str
    weight: float
    contribution: float
    reason: str
    observation_ids: tuple[UUID, ...]


class ScoredCandidate(GraphModel):
    case_id: UUID
    candidate_key: str
    left_observation_id: UUID
    right_observation_id: UUID
    status: CandidateStatus
    total_rules_score: float
    contributions: tuple[FeatureContribution, ...]
    contradiction_reasons: tuple[str, ...]
    supporting_observation_ids: tuple[UUID, ...]
    supporting_evidence_ids: tuple[UUID, ...]
    feature_snapshot_hash: str
    rules_config_version: str
    rules_config_hash: str
    explanation: str


class GraphEdgeSnapshot(GraphModel):
    case_id: UUID
    left_id: UUID
    right_id: UUID
    event_id: UUID
    evidence_observation_ids: tuple[UUID, ...]
    event_kind: str = "evidence_backed_event"
    event_start: datetime | None = None
    event_end: datetime | None = None


class MotifMatch(GraphModel):
    """A reviewable, source-backed event chain; never an asserted network fact."""

    case_id: UUID
    motif_id: str
    event_ids: tuple[UUID, UUID, UUID]
    supporting_observation_ids: tuple[UUID, ...]
    temporal_window_start: datetime
    temporal_window_end: datetime
    explanation: str


class AnalyticsResult(GraphModel):
    case_id: UUID
    algorithm: str
    configuration_hash: str
    graph_snapshot_hash: str
    run_at: datetime
    values: dict[str, float | int | str]
