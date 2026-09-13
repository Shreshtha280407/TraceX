"""Case-scoped, safe Phase 5 integration read API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import Extractor, SourceLocator
from app.modules.graph.api import (
    list_graph_candidates,
    list_graph_correlations,
    list_hypothesis_integration_refs,
)
from app.modules.graph.integration_models import (
    CORRELATION_EVENT_TYPE,
    INTEGRATION_SCHEMA_VERSION,
    CandidateLinkRecord,
    CorrelationRecord,
    EvidencePathSnapshot,
    GraphUpdateEventRecord,
    GraphUpdateEventStatus,
    PropositionStatus,
)

_NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _records(case_id):
    evidence_id, observation_id, correlation_id, event_id = (uuid4() for _ in range(4))
    path = EvidencePathSnapshot(
        evidence_id=evidence_id,
        observation_id=observation_id,
        source_locator=SourceLocator(page=1),
        extractor=Extractor(
            name="fixture", version="1", config_hash="fixture", model_version="n/a"
        ),
        observation_created_at=_NOW,
    )
    correlation = CorrelationRecord(
        correlation_id=correlation_id,
        case_id=case_id,
        idempotency_key="fixture.correlation",
        correlation_type="reviewable_fixture",
        status=PropositionStatus.CANDIDATE,
        supporting_observation_ids=(observation_id,),
        evidence_paths=(path,),
        mapping_version="mapping.v1",
        config_version="config.v1",
        hypothesis_reference="hypothesis-ref-1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    candidate = CandidateLinkRecord(
        candidate_link_id=uuid4(),
        correlation_id=correlation_id,
        case_id=case_id,
        idempotency_key="fixture.candidate",
        left_observation_id=observation_id,
        right_observation_id=observation_id,
        status=PropositionStatus.CANDIDATE,
        reason_reference="upstream.reason.v1",
        evidence_paths=(path,),
        created_at=_NOW,
    )
    event = GraphUpdateEventRecord(
        event_id=event_id,
        event_type=CORRELATION_EVENT_TYPE,
        schema_version=INTEGRATION_SCHEMA_VERSION,
        case_id=case_id,
        aggregate_type="correlation",
        aggregate_id=correlation_id,
        payload_reference=correlation_id,
        mapping_version="mapping.v1",
        config_version="config.v1",
        provenance_observation_ids=(observation_id,),
        projection_key="safe-projection-key",
        idempotency_key="fixture.correlation",
        status=GraphUpdateEventStatus.QUEUED,
        attempt=0,
        lease_expires_at=None,
        last_error_code=None,
        created_at=_NOW,
        updated_at=_NOW,
        completed_at=None,
    )
    return correlation, candidate, event


class _FakeIntegrationRepository:
    def __init__(self, case_id):
        self.case_id = case_id
        self.correlation, self.candidate, self.event = _records(case_id)

    async def list_correlations(self, case_id):
        return [self.correlation] if case_id == self.case_id else []

    async def get_correlation(self, case_id, correlation_id):
        if case_id == self.case_id and correlation_id == self.correlation.correlation_id:
            return self.correlation
        return None

    async def get_event_for_correlation(self, case_id, correlation_id):
        if case_id == self.case_id and correlation_id == self.correlation.correlation_id:
            return self.event
        return None

    async def list_candidates(self, case_id):
        return [self.candidate] if case_id == self.case_id else []


@pytest.fixture
def repository():
    case_id = uuid4()
    return _FakeIntegrationRepository(case_id)


async def test_integration_responses_are_case_scoped_safe_and_preserve_candidate_semantics(
    repository,
) -> None:
    correlations = await list_graph_correlations(repository.case_id, object(), repository)
    candidates = await list_graph_candidates(repository.case_id, object(), repository)
    hypotheses = await list_hypothesis_integration_refs(repository.case_id, object(), repository)
    other_case = await list_graph_correlations(uuid4(), object(), repository)

    assert correlations.items[0].correlation.status is PropositionStatus.CANDIDATE
    assert correlations.items[0].projection.status is GraphUpdateEventStatus.QUEUED
    assert candidates.items[0].status is PropositionStatus.CANDIDATE
    assert hypotheses.items[0].hypothesis_reference == "hypothesis-ref-1"
    assert other_case.items == ()
    for body in (correlations, candidates, hypotheses):
        lowered = str(body.model_dump(mode="json")).lower()
        assert "confirmed" not in lowered
        assert "object_uri" not in lowered
        assert "password" not in lowered
