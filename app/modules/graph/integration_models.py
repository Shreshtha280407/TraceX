"""Phase 5 durable correlation/candidate integration models.

These are internal, versioned integration records rather than additions to
the frozen ``app.contracts`` V1 worker payloads.  They deliberately model a
reviewable proposition, never a resolved identity or verified relationship.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.contracts.common import Extractor, SourceLocator
from app.modules.graph.models import GraphModel

INTEGRATION_SCHEMA_VERSION = "v1"
CORRELATION_EVENT_TYPE = "correlation.upserted.v1"


def _validate_feature_value(value: Any, forbidden: set[str]) -> None:
    """Reject source-like payloads at every nesting level, not only the root."""
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if any(token in normalized for token in forbidden):
                raise ValueError("feature snapshot contains a prohibited key")
            _validate_feature_value(nested, forbidden)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_feature_value(nested, forbidden)
    elif isinstance(value, str) and len(value) > 1_000:
        raise ValueError("feature snapshot string values must be bounded")


class PropositionStatus(StrEnum):
    """Lifecycle visible to consumers; none of these values asserts a fact."""

    CANDIDATE = "candidate"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class GraphUpdateEventStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"


class EvidencePathSnapshot(GraphModel):
    """A bounded, derived provenance snapshot; never raw evidence content."""

    evidence_id: UUID
    observation_id: UUID
    source_locator: SourceLocator
    extractor: Extractor
    observation_created_at: datetime
    event_time: datetime | None = None
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None


class FeatureSnapshotSubmission(GraphModel):
    """Optional reproducibility input supplied by an upstream intelligence producer."""

    snapshot_version: str = Field(min_length=1, max_length=128)
    config_hash: str = Field(min_length=1, max_length=256)
    values: dict[str, Any] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def reject_sensitive_feature_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"password", "secret", "token", "credential", "object_uri", "dsn"}
        _validate_feature_value(value, forbidden)
        return value


class CandidateLinkSubmission(GraphModel):
    """An evidence-backed candidate link between two canonical observations."""

    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    left_observation_id: UUID
    right_observation_id: UUID
    status: PropositionStatus = PropositionStatus.CANDIDATE
    reason_reference: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,256}$")


class CorrelationSubmission(GraphModel):
    """The minimal producer-facing write boundary, with no scoring fields."""

    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    correlation_type: str = Field(min_length=1, max_length=128)
    status: PropositionStatus = PropositionStatus.CANDIDATE
    supporting_observation_ids: tuple[UUID, ...] = Field(min_length=1)
    contradictory_observation_ids: tuple[UUID, ...] = ()
    candidate_links: tuple[CandidateLinkSubmission, ...] = ()
    feature_snapshot: FeatureSnapshotSubmission | None = None
    mapping_version: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=128)
    hypothesis_reference: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")

    @field_validator("supporting_observation_ids", "contradictory_observation_ids")
    @classmethod
    def reject_duplicate_observation_refs(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("observation references must be unique")
        return value

    @field_validator("candidate_links")
    @classmethod
    def reject_duplicate_candidate_keys(
        cls, value: tuple[CandidateLinkSubmission, ...]
    ) -> tuple[CandidateLinkSubmission, ...]:
        if len({candidate.idempotency_key for candidate in value}) != len(value):
            raise ValueError("candidate idempotency keys must be unique within a correlation")
        return value


class CandidateLinkRecord(GraphModel):
    candidate_link_id: UUID
    correlation_id: UUID
    case_id: UUID
    idempotency_key: str
    left_observation_id: UUID
    right_observation_id: UUID
    status: PropositionStatus
    reason_reference: str | None
    evidence_paths: tuple[EvidencePathSnapshot, ...]
    created_at: datetime


class FeatureSnapshotRecord(GraphModel):
    feature_snapshot_id: UUID
    correlation_id: UUID
    case_id: UUID
    snapshot_version: str
    config_hash: str
    values: dict[str, Any]
    created_at: datetime


class CorrelationRecord(GraphModel):
    correlation_id: UUID
    case_id: UUID
    idempotency_key: str
    correlation_type: str
    status: PropositionStatus
    supporting_observation_ids: tuple[UUID, ...]
    contradictory_observation_ids: tuple[UUID, ...] = ()
    evidence_paths: tuple[EvidencePathSnapshot, ...]
    mapping_version: str
    config_version: str
    hypothesis_reference: str | None = None
    created_at: datetime
    updated_at: datetime


class GraphUpdateEventRecord(GraphModel):
    event_id: UUID
    event_type: str
    schema_version: str
    case_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    payload_reference: UUID
    mapping_version: str
    config_version: str
    provenance_observation_ids: tuple[UUID, ...]
    projection_key: str
    idempotency_key: str
    status: GraphUpdateEventStatus
    attempt: int
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class CorrelationSubmissionReceipt(GraphModel):
    correlation: CorrelationRecord
    event: GraphUpdateEventRecord
    replayed: bool


class CorrelationIntegrationView(GraphModel):
    """Safe read shape: durable proposition plus asynchronous projection state."""

    correlation: CorrelationRecord
    projection: GraphUpdateEventRecord


class CorrelationListResponse(GraphModel):
    items: tuple[CorrelationIntegrationView, ...]


class CandidateListResponse(GraphModel):
    items: tuple[CandidateLinkRecord, ...]


class HypothesisIntegrationView(GraphModel):
    """An upstream reference only, expressly not a generated conclusion."""

    correlation_id: UUID
    hypothesis_reference: str
    status: PropositionStatus
    projection: GraphUpdateEventRecord


class HypothesisIntegrationListResponse(GraphModel):
    items: tuple[HypothesisIntegrationView, ...]


class CorrelationProjectionContext(GraphModel):
    """Canonical data handed to a future semantic projection handler."""

    event: GraphUpdateEventRecord
    correlation: CorrelationRecord
    candidates: tuple[CandidateLinkRecord, ...]
    feature_snapshot: FeatureSnapshotRecord | None
