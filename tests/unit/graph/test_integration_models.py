"""Contract-level safety rules for Phase 5 internal integration models."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.graph.integration_models import (
    CandidateLinkSubmission,
    CorrelationSubmission,
    FeatureSnapshotSubmission,
)


def test_candidate_link_has_no_verified_status() -> None:
    with pytest.raises(ValidationError):
        CandidateLinkSubmission(
            idempotency_key="candidate.1",
            left_observation_id=uuid4(),
            right_observation_id=uuid4(),
            status="verified",
        )


def test_feature_snapshot_rejects_secret_shaped_values() -> None:
    with pytest.raises(ValidationError, match="prohibited key"):
        FeatureSnapshotSubmission(
            snapshot_version="v1", config_hash="config", values={"token": "x"}
        )


def test_feature_snapshot_rejects_nested_secret_shaped_values() -> None:
    with pytest.raises(ValidationError, match="prohibited key"):
        FeatureSnapshotSubmission(
            snapshot_version="v1",
            config_hash="config",
            values={"nested": {"api_token": "x"}},
        )


def test_correlation_rejects_duplicate_candidate_idempotency_keys() -> None:
    first, second = uuid4(), uuid4()
    candidate = CandidateLinkSubmission(
        idempotency_key="candidate.1", left_observation_id=first, right_observation_id=second
    )
    with pytest.raises(ValidationError, match="candidate idempotency"):
        CorrelationSubmission(
            idempotency_key="correlation.1",
            correlation_type="fixture",
            supporting_observation_ids=(first,),
            candidate_links=(candidate, candidate),
            mapping_version="mapping.v1",
            config_version="config.v1",
        )
