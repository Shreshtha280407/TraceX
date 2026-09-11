"""Synthetic evidence/job record + upload builders for evidence-lifecycle tests.

Every value here is synthetic -- no real credentials or evidence content.
Mirrors `tests/fixtures/access_control/factories.py`'s pattern.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import UploadFile
from starlette.datastructures import Headers

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.worker import WorkerStatus
from app.modules.evidence_lifecycle.models import EvidenceRecord, WorkerJobRecord

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_upload_file(
    content: bytes = b"synthetic evidence bytes",
    filename: str | None = "evidence.txt",
    content_type: str = "text/plain",
) -> UploadFile:
    headers = Headers({"content-type": content_type}) if content_type else Headers({})
    return UploadFile(file=io.BytesIO(content), filename=filename, headers=headers)


def make_evidence_record(**overrides: Any) -> EvidenceRecord:
    data: dict[str, Any] = {
        "evidence_id": uuid4(),
        "case_id": uuid4(),
        "source_type": SourceType.DOCUMENT,
        "original_filename": "evidence.txt",
        "content_type": "text/plain",
        "object_uri": "cases/00000000-0000-0000-0000-000000000000/evidence/"
        "11111111-1111-1111-1111-111111111111/original",
        "sha256": hashlib.sha256(b"synthetic").hexdigest(),
        "classification": EvidenceClassification.UNCLASSIFIED,
        "uploaded_by": uuid4(),
        "uploaded_at": FIXED_TIME,
        "parser_profile": None,
        "processing_status": EvidenceProcessingStatus.QUEUED,
        "upload_idempotency_key": None,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    data.update(overrides)
    return EvidenceRecord(**data)


def make_job_record(**overrides: Any) -> WorkerJobRecord:
    case_id = overrides.get("case_id", uuid4())
    evidence_id = overrides.get("evidence_id", uuid4())
    processor_name = overrides.get("processor_name", "fir_report_text_v1")
    processor_version = overrides.get("processor_version", "1.0.0")
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "case_id": case_id,
        "evidence_id": evidence_id,
        "source_type": SourceType.DOCUMENT,
        "processor_name": processor_name,
        "processor_version": processor_version,
        "attempt": 1,
        "idempotency_key": f"{case_id}:{evidence_id}:{processor_name}:{processor_version}",
        "input_object_uri": "cases/x/evidence/y/original",
        "requested_at": FIXED_TIME,
        "status": WorkerStatus.QUEUED,
        "queued_at": FIXED_TIME,
        "dispatched_at": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "claimed_by": None,
        "claimed_by_worker_id": None,
        "claim_token_hash": None,
        "last_error_code": None,
        "last_error_message": None,
        "last_error_retryable": None,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    data.update(overrides)
    return WorkerJobRecord(**data)
