"""Contract tests for ObservationBatchSubmissionV1 / TransformationProvenanceV1 /
ObservationBatchProgressV1 (Phase 3, Nipun).

See docs/qa/test-matrix.md (Phase 3 entries) and docs/architecture/phase-3-decisions.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.contracts.common import BoundingBoxNormalized
from app.contracts.observation_batch import (
    ObservationBatchProgressV1,
    ObservationBatchSubmissionV1,
    TransformationProvenanceV1,
)
from tests.fixtures.factories import (
    FIXED_TIME,
    assert_roundtrips,
    make_batch_progress,
    make_observation,
    make_observation_batch_submission,
    make_source_locator,
    make_transformation_provenance,
)

# --- Scenarios 1-4: valid modality-specific batches parse -------------------


def test_valid_document_page_span_observation_batch_parses() -> None:
    observation = make_observation(
        observation_type="document_text_mention",
        source_locator=make_source_locator(page=3, span_start=10, span_end=42),
    )
    batch = make_observation_batch_submission(
        case_id=observation.case_id, evidence_id=observation.evidence_id, observations=[observation]
    )
    assert batch.observations[0].source_locator.page == 3
    assert_roundtrips(batch)


def test_valid_scanned_document_bbox_observation_batch_parses() -> None:
    observation = make_observation(
        observation_type="ocr_text_mention",
        source_locator=make_source_locator(
            page=1,
            span_start=None,
            span_end=None,
            bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.6),
        ),
    )
    batch = make_observation_batch_submission(
        case_id=observation.case_id, evidence_id=observation.evidence_id, observations=[observation]
    )
    assert batch.observations[0].source_locator.bbox_xyxy_normalized is not None
    assert_roundtrips(batch)


def test_valid_cdr_row_column_observation_batch_parses() -> None:
    observation = make_observation(
        observation_type="cdr_call_record",
        source_locator=make_source_locator(
            page=None, span_start=None, span_end=None, row=5, column=2
        ),
    )
    batch = make_observation_batch_submission(
        case_id=observation.case_id, evidence_id=observation.evidence_id, observations=[observation]
    )
    assert batch.observations[0].source_locator.row == 5
    assert_roundtrips(batch)


def test_valid_finance_sheet_row_cell_observation_batch_parses() -> None:
    observation = make_observation(
        observation_type="financial_transaction_record",
        source_locator=make_source_locator(
            page=None, span_start=None, span_end=None, sheet="Sheet1", row=10, column=3
        ),
    )
    batch = make_observation_batch_submission(
        case_id=observation.case_id, evidence_id=observation.evidence_id, observations=[observation]
    )
    assert batch.observations[0].source_locator.sheet == "Sheet1"
    assert_roundtrips(batch)


# --- Scenario 5: invalid ObservationV1 fields rejected through batch submission --


def test_batch_rejects_observation_with_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(
            observations=[make_observation(extraction_confidence=1.5)]
        )


def test_batch_rejects_observation_with_invalid_bbox() -> None:
    with pytest.raises(ValidationError):
        BoundingBoxNormalized(x_min=0.5, y_min=0.0, x_max=0.1, y_max=1.0)


def test_batch_rejects_observation_with_invalid_time_range_locator() -> None:
    with pytest.raises(ValidationError):
        make_observation(source_locator=make_source_locator(time_start_ms=100, time_end_ms=50))


def test_batch_rejects_observation_with_locator_carrying_no_fields() -> None:
    with pytest.raises(ValidationError):
        make_observation(
            source_locator=make_source_locator(page=None, span_start=None, span_end=None)
        )


# --- Scenario 6: invalid/missing batch_id/sequence/timestamps/idempotency_key ---


@pytest.mark.parametrize("bad_batch_id", ["", "has spaces", "x" * 129])
def test_batch_rejects_malformed_batch_id(bad_batch_id: str) -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(batch_id=bad_batch_id)


@pytest.mark.parametrize("bad_key", ["", "has spaces", "x" * 129])
def test_batch_rejects_malformed_idempotency_key(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(idempotency_key=bad_key)


def test_batch_rejects_negative_batch_sequence() -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(batch_sequence=-1, progress=None, transformations=[])


def test_batch_rejects_naive_submitted_at() -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(submitted_at=datetime(2026, 1, 1, 12, 0, 0))


def test_batch_rejects_duplicate_observation_id_within_one_batch() -> None:
    observation = make_observation()
    with pytest.raises(ValidationError):
        make_observation_batch_submission(
            case_id=observation.case_id,
            evidence_id=observation.evidence_id,
            observations=[observation, observation],
        )


def test_batch_rejects_observation_with_mismatched_case_or_evidence() -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(observations=[make_observation()])


def test_entirely_empty_batch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(observations=[], transformations=[], progress=None)


def test_valid_batch_with_only_progress_and_no_observations_is_accepted() -> None:
    batch = make_observation_batch_submission(observations=[], transformations=[])
    assert batch.observations == []
    assert batch.progress is not None


# --- Scenario 7: transformation provenance safety/ordering/status ----------


def test_transformation_rejects_secret_bearing_metadata_key() -> None:
    with pytest.raises(ValidationError):
        make_transformation_provenance(safe_metadata={"api_token": "abc123"})


@pytest.mark.parametrize(
    "bad_key", ["password", "worker_secret", "STDERR_dump", "object_uri", "PRIVATE_KEY"]
)
def test_transformation_rejects_various_secret_like_metadata_keys(bad_key: str) -> None:
    with pytest.raises(ValidationError):
        make_transformation_provenance(safe_metadata={bad_key: "value"})


def test_transformation_rejects_overly_long_metadata_string() -> None:
    with pytest.raises(ValidationError):
        make_transformation_provenance(safe_metadata={"note": "x" * 501})


def test_transformation_allows_short_safe_metadata() -> None:
    transformation = make_transformation_provenance(safe_metadata={"page_count": 12, "note": "ok"})
    assert transformation.safe_metadata["page_count"] == 12


def test_transformation_rejects_completed_before_started() -> None:
    with pytest.raises(ValidationError):
        make_transformation_provenance(
            started_at=FIXED_TIME, completed_at=FIXED_TIME - timedelta(seconds=1)
        )


def test_transformation_rejects_negative_ordinal() -> None:
    with pytest.raises(ValidationError):
        make_transformation_provenance(ordinal=-1)


def test_transformation_rejects_unknown_status_value() -> None:
    valid = make_transformation_provenance().model_dump(mode="json")
    valid["status"] = "in_progress"
    with pytest.raises(ValidationError):
        TransformationProvenanceV1(**valid)


def test_batch_rejects_duplicate_transformation_ordinal() -> None:
    observation = make_observation()
    job_id = uuid4()
    scope = {
        "case_id": observation.case_id,
        "evidence_id": observation.evidence_id,
        "job_id": job_id,
        "batch_id": "batch-1",
    }
    with pytest.raises(ValidationError):
        make_observation_batch_submission(
            job_id=job_id,
            case_id=observation.case_id,
            evidence_id=observation.evidence_id,
            batch_id="batch-1",
            observations=[observation],
            transformations=[
                make_transformation_provenance(ordinal=0, **scope),
                make_transformation_provenance(ordinal=0, **scope),
            ],
        )


def test_batch_rejects_transformation_output_id_not_in_batch() -> None:
    observation = make_observation()
    job_id = uuid4()
    with pytest.raises(ValidationError):
        make_observation_batch_submission(
            job_id=job_id,
            case_id=observation.case_id,
            evidence_id=observation.evidence_id,
            batch_id="batch-1",
            observations=[observation],
            transformations=[
                make_transformation_provenance(
                    job_id=job_id,
                    case_id=observation.case_id,
                    evidence_id=observation.evidence_id,
                    batch_id="batch-1",
                    output_observation_ids=[uuid4()],
                )
            ],
        )


def test_batch_rejects_transformation_with_mismatched_scope() -> None:
    observation = make_observation()
    job_id = uuid4()
    with pytest.raises(ValidationError):
        make_observation_batch_submission(
            job_id=job_id,
            case_id=observation.case_id,
            evidence_id=observation.evidence_id,
            batch_id="batch-1",
            observations=[observation],
            transformations=[
                make_transformation_provenance(
                    job_id=job_id,
                    case_id=observation.case_id,
                    evidence_id=observation.evidence_id,
                    batch_id="a-different-batch",
                )
            ],
        )


# --- Scenario 8: progress validation ----------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("units_total", -1),
        ("units_completed", -1),
        ("observations_emitted", -1),
        ("batch_sequence", -1),
    ],
)
def test_progress_rejects_negative_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_batch_progress(**{field: value})


def test_progress_rejects_units_completed_exceeding_units_total() -> None:
    with pytest.raises(ValidationError):
        make_batch_progress(units_total=5, units_completed=6)


def test_progress_rejects_invalid_stage_format() -> None:
    with pytest.raises(ValidationError):
        make_batch_progress(stage="Not Valid")


def test_progress_rejects_invalid_message_code_format() -> None:
    with pytest.raises(ValidationError):
        make_batch_progress(message_code="not_valid")


def test_progress_batch_sequence_must_match_submission_batch_sequence() -> None:
    with pytest.raises(ValidationError):
        make_observation_batch_submission(
            batch_sequence=1, progress=make_batch_progress(batch_sequence=0)
        )


def test_progress_roundtrips() -> None:
    assert_roundtrips(make_batch_progress())


def test_valid_progress_parses() -> None:
    progress = make_batch_progress()
    assert isinstance(progress, ObservationBatchProgressV1)


def test_observation_batch_submission_roundtrips_with_transformations() -> None:
    observation = make_observation()
    job_id = uuid4()
    transformation = make_transformation_provenance(
        job_id=job_id,
        case_id=observation.case_id,
        evidence_id=observation.evidence_id,
        batch_id="batch-1",
        output_observation_ids=[observation.observation_id],
    )
    batch = make_observation_batch_submission(
        job_id=job_id,
        case_id=observation.case_id,
        evidence_id=observation.evidence_id,
        batch_id="batch-1",
        observations=[observation],
        transformations=[transformation],
    )
    assert isinstance(batch, ObservationBatchSubmissionV1)
    assert_roundtrips(batch)
