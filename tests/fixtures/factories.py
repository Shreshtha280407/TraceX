"""Valid contract instance builders shared across the test suite.

Each `make_*` function returns a fully-valid instance with sensible
defaults; pass keyword overrides to construct edge cases for a specific
test without repeating every other required field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.contracts.common import Extractor, Location, SourceLocator, TraceXModel
from app.contracts.entity import EntityV1
from app.contracts.event import EventV1
from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
    SourceType,
)
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.contracts.observation_batch import (
    ObservationBatchProgressV1,
    ObservationBatchSubmissionV1,
    TransformationProvenanceV1,
    TransformationStatus,
)
from app.contracts.worker import WorkerJobV1, WorkerResultV1, WorkerStatus

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def assert_roundtrips(model: TraceXModel) -> None:
    """Serialize to JSON and back; the reloaded model must equal the original."""
    reloaded = type(model).model_validate_json(model.model_dump_json())
    assert reloaded == model


def make_source_locator(**overrides: Any) -> SourceLocator:
    data: dict[str, Any] = {"page": 1, "span_start": 0, "span_end": 42}
    data.update(overrides)
    return SourceLocator(**data)


def make_extractor(**overrides: Any) -> Extractor:
    data: dict[str, Any] = {
        "name": "pdf-text-extractor",
        "version": "1.0.0",
        "config_hash": "c" * 16,
        "model_version": "n/a",
    }
    data.update(overrides)
    return Extractor(**data)


def make_evidence_record(**overrides: Any) -> EvidenceRecordV1:
    data: dict[str, Any] = {
        "evidence_id": uuid4(),
        "case_id": uuid4(),
        "source_type": SourceType.DOCUMENT,
        "original_filename": "statement.pdf",
        "content_type": "application/pdf",
        "object_uri": "s3://tracex-evidence/case-1/statement.pdf",
        "sha256": "a" * 64,
        "classification": EvidenceClassification.RESTRICTED,
        "uploaded_by": "analyst-1",
        "uploaded_at": FIXED_TIME,
        "parser_profile": None,
        "processing_status": EvidenceProcessingStatus.UPLOADED,
    }
    data.update(overrides)
    return EvidenceRecordV1(**data)


def make_observation(**overrides: Any) -> ObservationV1:
    data: dict[str, Any] = {
        "observation_id": uuid4(),
        "case_id": uuid4(),
        "evidence_id": uuid4(),
        "observation_type": "document_text_mention",
        "extracted_entities": [ExtractedEntityMention(text="John Doe", entity_type_hint="person")],
        "event_time": FIXED_TIME,
        "time_window": None,
        "location": Location(raw_text="Mumbai"),
        "attributes": {"language": "en"},
        "extraction_confidence": 0.87,
        "source_locator": make_source_locator(),
        "extractor": make_extractor(),
        "created_at": FIXED_TIME,
    }
    data.update(overrides)
    return ObservationV1(**data)


def make_entity(**overrides: Any) -> EntityV1:
    data: dict[str, Any] = {
        "entity_id": uuid4(),
        "case_id": uuid4(),
        "entity_type": "person",
        "canonical_label": "John Doe",
        "aliases": ["J. Doe"],
        "stable_identifiers": {"phone": "+911234567890"},
        "attributes": {},
        "created_from_observation_ids": [uuid4()],
        "created_at": FIXED_TIME,
    }
    data.update(overrides)
    return EntityV1(**data)


def make_event(**overrides: Any) -> EventV1:
    data: dict[str, Any] = {
        "event_id": uuid4(),
        "case_id": uuid4(),
        "event_type": "call",
        "participant_entity_ids": [uuid4(), uuid4()],
        "event_time": FIXED_TIME,
        "time_window": None,
        "location": None,
        "attributes": {"duration_seconds": 120},
        "evidence_refs": [uuid4()],
        "confidence": 0.9,
        "created_at": FIXED_TIME,
    }
    data.update(overrides)
    return EventV1(**data)


def make_worker_job(**overrides: Any) -> WorkerJobV1:
    case_id = overrides.pop("case_id", uuid4())
    evidence_id = overrides.pop("evidence_id", uuid4())
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "case_id": case_id,
        "evidence_id": evidence_id,
        "source_type": SourceType.CDR,
        "processor_name": "cdr-parser",
        "processor_version": "1.0.0",
        "attempt": 1,
        "idempotency_key": f"{case_id}:{evidence_id}:cdr-parser:1.0.0",
        "input_object_uri": "s3://tracex-evidence/case-1/cdr.csv",
        "requested_at": FIXED_TIME,
    }
    data.update(overrides)
    return WorkerJobV1(**data)


def make_batch_progress(**overrides: Any) -> ObservationBatchProgressV1:
    data: dict[str, Any] = {
        "stage": "parsing",
        "units_total": 10,
        "units_completed": 1,
        "observations_emitted": 1,
        "batch_sequence": 0,
        "message_code": "PAGE_PARSED",
        "occurred_at": FIXED_TIME,
    }
    data.update(overrides)
    return ObservationBatchProgressV1(**data)


def make_transformation_provenance(**overrides: Any) -> TransformationProvenanceV1:
    data: dict[str, Any] = {
        "transformation_id": uuid4(),
        "case_id": uuid4(),
        "evidence_id": uuid4(),
        "job_id": uuid4(),
        "batch_id": "batch-1",
        "ordinal": 0,
        "step_name": "pdf_text_extraction",
        "step_version": "1.0.0",
        "config_hash": "c" * 16,
        "model_version": "n/a",
        "input_locator": make_source_locator(),
        "output_observation_ids": [],
        "derived_artifact_refs": [],
        "status": TransformationStatus.SUCCEEDED,
        "started_at": FIXED_TIME,
        "completed_at": FIXED_TIME,
        "safe_metadata": {},
    }
    data.update(overrides)
    return TransformationProvenanceV1(**data)


def make_observation_batch_submission(**overrides: Any) -> ObservationBatchSubmissionV1:
    """A valid single-observation batch, defaulting `progress.batch_sequence` to match
    `batch_sequence` (the contract requires the two to agree) -- override both together
    if you need a non-zero sequence."""
    case_id = overrides.pop("case_id", uuid4())
    evidence_id = overrides.pop("evidence_id", uuid4())
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "case_id": case_id,
        "evidence_id": evidence_id,
        "batch_id": "batch-1",
        "batch_sequence": 0,
        "idempotency_key": "idem-1",
        "observations": [make_observation(case_id=case_id, evidence_id=evidence_id)],
        "transformations": [],
        "progress": make_batch_progress(batch_sequence=0),
        "submitted_at": FIXED_TIME,
        "is_final_batch": False,
    }
    data.update(overrides)
    return ObservationBatchSubmissionV1(**data)


def make_worker_result(**overrides: Any) -> WorkerResultV1:
    data: dict[str, Any] = {
        "job_id": uuid4(),
        "case_id": uuid4(),
        "evidence_id": uuid4(),
        "status": WorkerStatus.SUCCEEDED,
        "observations": [make_observation()],
        "derived_artifacts": [],
        "checkpoint": None,
        "error": None,
        "completed_at": FIXED_TIME,
    }
    data.update(overrides)
    return WorkerResultV1(**data)
