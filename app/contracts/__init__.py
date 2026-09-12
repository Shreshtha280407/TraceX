"""Versioned shared contracts for TraceX.

`CONTRACT_VERSIONS` is the single source of truth for which contract
versions this API build supports; it backs `GET /api/v1/meta/contracts` and
is asserted against directly in contract tests so the two can never drift.
"""

from __future__ import annotations

from app.contracts.entity import EntityV1
from app.contracts.event import EventV1
from app.contracts.evidence import EvidenceRecordV1
from app.contracts.observation import ObservationV1
from app.contracts.observation_batch import (
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
    TransformationProvenanceV1,
)
from app.contracts.worker import WorkerJobV1, WorkerProgressV1, WorkerResultV1

CONTRACT_VERSIONS: dict[str, str] = {
    "evidence_record": EvidenceRecordV1.__name__,
    "observation": ObservationV1.__name__,
    "entity": EntityV1.__name__,
    "event": EventV1.__name__,
    "worker_job": WorkerJobV1.__name__,
    "worker_result": WorkerResultV1.__name__,
    "observation_batch_submission": ObservationBatchSubmissionV1.__name__,
    "observation_batch_receipt": ObservationBatchReceiptV1.__name__,
    "transformation_provenance": TransformationProvenanceV1.__name__,
}

__all__ = [
    "CONTRACT_VERSIONS",
    "EntityV1",
    "EvidenceRecordV1",
    "EventV1",
    "ObservationBatchReceiptV1",
    "ObservationBatchSubmissionV1",
    "ObservationV1",
    "TransformationProvenanceV1",
    "WorkerJobV1",
    "WorkerProgressV1",
    "WorkerResultV1",
]
