"""EvidenceRecordV1 / WorkerJobV1 pair builders for media-processing tests.

Mirrors `tests/fixtures/structured_processing/factory.py` exactly, with
`SourceType.VIDEO`/`IMAGE` as the default and this module's processor names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
    SourceType,
)
from app.contracts.worker import WorkerJobV1

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_evidence_and_job(
    *,
    content_type: str,
    filename: str,
    processor_name: str,
    processor_version: str = "1.0.0",
    source_type: SourceType = SourceType.VIDEO,
    case_id: UUID | None = None,
    evidence_id: UUID | None = None,
) -> tuple[EvidenceRecordV1, WorkerJobV1]:
    case_id = case_id or uuid4()
    evidence_id = evidence_id or uuid4()

    evidence = EvidenceRecordV1(
        evidence_id=evidence_id,
        case_id=case_id,
        source_type=source_type,
        original_filename=filename,
        content_type=content_type,
        object_uri=f"local://{filename}",
        sha256="a" * 64,
        classification=EvidenceClassification.RESTRICTED,
        uploaded_by="tester",
        uploaded_at=FIXED_TIME,
        processing_status=EvidenceProcessingStatus.UPLOADED,
    )
    job = WorkerJobV1(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=source_type,
        processor_name=processor_name,
        processor_version=processor_version,
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:{processor_name}:{processor_version}",
        input_object_uri=f"local://{filename}",
        requested_at=FIXED_TIME,
    )
    return evidence, job
