"""WorkerJobV1 builders for communication-processing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerJobV1

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_job(
    *,
    processor_name: str,
    source_type: SourceType,
    processor_version: str = "1.0.0",
    case_id: UUID | None = None,
    evidence_id: UUID | None = None,
) -> WorkerJobV1:
    case_id = case_id or uuid4()
    evidence_id = evidence_id or uuid4()
    return WorkerJobV1(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=source_type,
        processor_name=processor_name,
        processor_version=processor_version,
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:{processor_name}:{processor_version}",
        input_object_uri="local://test-input",
        requested_at=FIXED_TIME,
    )
