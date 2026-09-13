"""Unit tests for ``app.modules.media_processing.ocr_batching`` (Phase 3 — Gaurav).

Scenarios 17-23 from the Phase 3 test matrix:

17.  ``iter_image_ocr_batches`` produces correct batch structure with provenance
18.  Video-frame batch has ``frame_number``, ``time_start_ms``, ``time_end_ms``,
     and ``bbox_xyxy_normalized`` in the source locator
19.  ``TransformationProvenanceV1.output_observation_ids`` references only the
     batch's own observations (contract-enforced)
20.  Safe metadata in ``TransformationProvenanceV1`` has no forbidden keys
21.  ``build_terminal_result`` produces ``observations=[]`` with status ``succeeded``
22.  Idempotency key is per-(job, batch_sequence) and is stable across replays
23.  ``FixtureOcrAdapter`` → ``iter_image_ocr_batches`` end-to-end: observations
     land in the batch with the right ``ocr_text_mention`` type and locator
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.observation_batch import ObservationBatchSubmissionV1
from app.contracts.worker import WorkerStatus
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import ExtractedFrame, ImageMetadata, VideoMetadata
from app.modules.media_processing.ocr_adapter import (
    FIXTURE_TEXT_PREFIX,
    FixtureOcrAdapter,
    OcrBoxResult,
    OcrPreprocessTransform,
)
from app.modules.media_processing.ocr_batching import (
    STEP_NAME_IMAGE_OCR,
    STEP_NAME_VIDEO_FRAME_OCR,
    OcrBatchConfig,
    build_terminal_result,
    build_terminal_result_failed,
    build_video_frame_ocr_batch,
    iter_image_ocr_batches,
)
from tests.fixtures.media_processing.factory import make_evidence_and_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_job(*, source_type: str = "image"):
    from app.contracts.evidence import SourceType

    st = SourceType.IMAGE if source_type == "image" else SourceType.VIDEO
    _, job = make_evidence_and_job(
        content_type="image/png",
        filename="test.png",
        processor_name="media_detection_v1",
        processor_version="1.0.0",
        source_type=st,
    )
    return job


def _make_ocr_result(
    text: str = "TEST",
    *,
    confidence: float = 0.85,
    width: int = 400,
    height: int = 200,
) -> OcrBoxResult:
    # Use text length and hash to pseudo-randomize box to ensure distinct locators
    h = abs(hash(text))
    x_min = 10.0 + (h % 100)
    y_min = 20.0 + ((h // 100) % 100)
    x_max = x_min + 50.0
    y_max = y_min + 20.0

    pixel_box = PixelBoundingBox(x_min=x_min, y_min=y_min, x_max=x_max, y_max=y_max)
    from app.modules.media_processing.image.geometry import to_normalized

    normalized = to_normalized(pixel_box, image_width=width, image_height=height)
    return OcrBoxResult(
        text=text,
        confidence=confidence,
        pixel_box=pixel_box,
        bbox_xyxy_normalized=normalized,
        source_image_width=width,
        source_image_height=height,
        ocr_unit="line",
        ocr_engine_name="tesseract_ocr_v1",
        ocr_engine_version="5.3.0",
        language="eng",
        config_hash="deadbeef01234567",
        preprocessing_version="media_ocr_adapter_v1",
        preprocess_transform=OcrPreprocessTransform(),
    )


def _make_extracted_frame(
    *,
    frame_number: int | None = 5,
    time_start_ms: int = 1000,
    time_end_ms: int = 1200,
    width: int = 400,
    height: int = 200,
) -> ExtractedFrame:
    import numpy as np

    return ExtractedFrame(
        image=np.zeros((height, width, 3), dtype=np.uint8),
        frame_number=frame_number,
        time_start_ms=time_start_ms,
        time_end_ms=time_end_ms,
        width=width,
        height=height,
    )


def _make_video_metadata(width: int = 400, height: int = 200) -> VideoMetadata:
    return VideoMetadata(
        container_format="mp4",
        duration_ms=10000,
        width=width,
        height=height,
        frame_rate=25.0,
        frame_count=250,
        video_codec="h264",
        has_audio=False,
        rotation_degrees=0,
    )


# ---------------------------------------------------------------------------
# Scenario 17: iter_image_ocr_batches basic structure
# ---------------------------------------------------------------------------


def test_iter_image_ocr_batches_single_batch_structure() -> None:
    """Scenario 17: single batch has correct job/case/evidence IDs, observations, provenance."""
    job = _make_job()
    results = [_make_ocr_result(f"TEXT_{i}") for i in range(3)]

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix=f"{job.job_id}-ocr",
            batch_config=OcrBatchConfig(batch_size=10, units_total=3),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    assert len(batches) == 1, "3 results in batch_size=10 → exactly 1 batch"

    batch = batches[0]
    assert batch.job_id == job.job_id
    assert batch.case_id == job.case_id
    assert batch.evidence_id == job.evidence_id
    assert len(batch.observations) == 3
    assert len(batch.transformations) == 1
    assert batch.is_final_batch is True
    assert batch.batch_sequence == 0


def test_iter_image_ocr_batches_chunking() -> None:
    """Scenario 17b: 5 results with batch_size=2 → 3 batches."""
    job = _make_job()
    results = [_make_ocr_result(f"TEXT_{i}") for i in range(5)]

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix=f"{job.job_id}-ocr",
            batch_config=OcrBatchConfig(batch_size=2),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    assert len(batches) == 3
    assert batches[0].batch_sequence == 0
    assert batches[1].batch_sequence == 1
    assert batches[2].batch_sequence == 2
    assert batches[2].is_final_batch is True
    assert not batches[0].is_final_batch
    assert not batches[1].is_final_batch


def test_iter_image_ocr_batches_empty_results_yields_one_empty_batch() -> None:
    """No valid results → yields one empty batch (progress signal, not silence)."""
    job = _make_job()
    batches = list(
        iter_image_ocr_batches(
            [],
            job=job,
            idempotency_key_prefix="empty",
            batch_config=OcrBatchConfig(batch_size=10),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    assert len(batches) == 1
    assert batches[0].observations == []


# ---------------------------------------------------------------------------
# Scenario 18: Video-frame locator fields
# ---------------------------------------------------------------------------


def test_video_frame_ocr_batch_locator_has_all_required_fields() -> None:
    """Scenario 18: video-frame batch locator has frame_number, time bounds, bbox."""
    job = _make_job(source_type="video")
    frame = _make_extracted_frame(frame_number=5, time_start_ms=1000, time_end_ms=1200)
    meta = _make_video_metadata()
    results = [_make_ocr_result("FRAME TEXT")]

    batch = build_video_frame_ocr_batch(
        results,
        job=job,
        frame=frame,
        metadata=meta,
        batch_id="frame-5",
        batch_sequence=5,
        idempotency_key="idem-frame-5",
        units_completed=6,
        units_total=20,
        observations_emitted_before=5,
        started_at=FIXED_TIME,
        completed_at=FIXED_TIME,
    )

    assert len(batch.observations) == 1
    obs = batch.observations[0]
    loc = obs.source_locator
    assert loc.frame_number == 5
    assert loc.time_start_ms == 1000
    assert loc.time_end_ms == 1200
    assert loc.bbox_xyxy_normalized is not None
    assert 0.0 <= loc.bbox_xyxy_normalized.x_min < loc.bbox_xyxy_normalized.x_max <= 1.0
    assert 0.0 <= loc.bbox_xyxy_normalized.y_min < loc.bbox_xyxy_normalized.y_max <= 1.0


def test_video_frame_ocr_batch_observation_type_is_ocr_text_mention() -> None:
    """Video-frame observations must be 'ocr_text_mention'."""
    job = _make_job(source_type="video")
    frame = _make_extracted_frame()
    meta = _make_video_metadata()
    results = [_make_ocr_result("FRAME TEXT")]

    batch = build_video_frame_ocr_batch(
        results,
        job=job,
        frame=frame,
        metadata=meta,
        batch_id="f0",
        batch_sequence=0,
        idempotency_key="ik-f0",
        units_completed=1,
        units_total=1,
        observations_emitted_before=0,
        started_at=FIXED_TIME,
        completed_at=FIXED_TIME,
        is_final=True,
    )
    for obs in batch.observations:
        assert obs.observation_type == "ocr_text_mention"


# ---------------------------------------------------------------------------
# Scenario 19: TransformationProvenanceV1.output_observation_ids ⊆ batch obs
# ---------------------------------------------------------------------------


def test_transformation_output_ids_are_subset_of_batch_observations() -> None:
    """Scenario 19: output_observation_ids must only reference the batch's own observations.

    ObservationBatchSubmissionV1's own model validator enforces this at
    serialization time -- we verify it doesn't raise during batch construction.
    """
    job = _make_job()
    results = [_make_ocr_result(f"TEXT_{i}") for i in range(3)]

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="test",
            batch_config=OcrBatchConfig(batch_size=10),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    batch = batches[0]
    obs_ids = {o.observation_id for o in batch.observations}
    for transform in batch.transformations:
        for ref_id in transform.output_observation_ids:
            assert ref_id in obs_ids, (
                f"transformation references obs {ref_id} which is not in batch observations"
            )


def test_transformation_output_ids_foreign_reference_would_fail() -> None:
    """Attempting to construct a batch with a dangling provenance reference fails validation."""
    job = _make_job()
    results = [_make_ocr_result("TEXT")]
    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="test",
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    batch = batches[0]
    # Inject a dangling ID into the provenance manually -- batch's validator must catch it.
    dangling_id = uuid4()
    modified_transform = batch.transformations[0].model_copy(
        update={"output_observation_ids": [dangling_id]}
    )
    from pydantic_core._pydantic_core import ValidationError

    with pytest.raises(ValidationError):
        ObservationBatchSubmissionV1(
            job_id=batch.job_id,
            case_id=batch.case_id,
            evidence_id=batch.evidence_id,
            batch_id=batch.batch_id,
            batch_sequence=batch.batch_sequence,
            idempotency_key=batch.idempotency_key,
            observations=batch.observations,
            transformations=[modified_transform],
            progress=batch.progress,
            submitted_at=batch.submitted_at,
            is_final_batch=batch.is_final_batch,
        )


# ---------------------------------------------------------------------------
# Scenario 20: Safe metadata keys/values
# ---------------------------------------------------------------------------


_FORBIDDEN_SAFE_META_KEYS = {
    "password",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "stderr",
    "stacktrace",
    "traceback",
    "object_uri",
    "filepath",
    "file_path",
    "dsn",
    "private_key",
}


def test_transformation_provenance_safe_metadata_has_no_forbidden_keys() -> None:
    """Scenario 20: safe_metadata produced by iter_image_ocr_batches has no forbidden keys."""
    job = _make_job()
    results = [_make_ocr_result("TEST")]

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="sec-test",
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    for batch in batches:
        for transform in batch.transformations:
            if transform.safe_metadata:
                for key in transform.safe_metadata:
                    key_lower = key.lower()
                    for forbidden in _FORBIDDEN_SAFE_META_KEYS:
                        assert forbidden not in key_lower, (
                            f"safe_metadata key '{key}' contains forbidden substring '{forbidden}'"
                        )


def test_transformation_provenance_safe_metadata_string_values_under_500_chars() -> None:
    """All string values in safe_metadata must be <= 500 chars."""
    job = _make_job()
    results = [_make_ocr_result("TEST")]

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="len-test",
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    for batch in batches:
        for transform in batch.transformations:
            if transform.safe_metadata:
                for val in transform.safe_metadata.values():
                    if isinstance(val, str):
                        assert len(val) <= 500, f"safe_metadata string value too long: {val!r}"


# ---------------------------------------------------------------------------
# Scenario 21: build_terminal_result
# ---------------------------------------------------------------------------


def test_build_terminal_result_has_empty_observations() -> None:
    """Scenario 21: terminal result has observations=[] as required."""
    job = _make_job()
    result = build_terminal_result(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        completed_at=FIXED_TIME,
    )
    assert result.observations == [], "terminal result must carry no observations"
    assert result.status == WorkerStatus.SUCCEEDED
    assert result.job_id == job.job_id
    assert result.error is None


def test_build_terminal_result_failed_has_error() -> None:
    """Failed terminal result carries an error and status FAILED."""
    job = _make_job()
    result = build_terminal_result_failed(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        error_code="media_decode_failed",
        error_message="synthetic decode error",
        retryable=False,
    )
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "media_decode_failed"
    assert result.observations == []


# ---------------------------------------------------------------------------
# Scenario 22: idempotency keys are stable across replays
# ---------------------------------------------------------------------------


def test_idempotency_keys_are_stable_across_identical_replays() -> None:
    """Scenario 22: replaying the same results → same idempotency keys."""
    job = _make_job()
    results = [_make_ocr_result(f"TEXT_{i}") for i in range(4)]
    prefix = f"{job.job_id}-idem"

    batches_first = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix=prefix,
            batch_config=OcrBatchConfig(batch_size=2),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    batches_second = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix=prefix,
            batch_config=OcrBatchConfig(batch_size=2),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    assert len(batches_first) == len(batches_second)
    for b1, b2 in zip(batches_first, batches_second, strict=False):
        assert b1.idempotency_key == b2.idempotency_key
        assert b1.batch_id == b2.batch_id


# ---------------------------------------------------------------------------
# Scenario 23: FixtureOcrAdapter → iter_image_ocr_batches end-to-end
# ---------------------------------------------------------------------------


def test_fixture_adapter_to_batch_produces_ocr_text_mention_observations() -> None:
    """Scenario 23: observations from FixtureOcrAdapter land with ocr_text_mention type."""
    import numpy as np

    job = _make_job()
    adapter = FixtureOcrAdapter(confidence=0.75)
    meta = ImageMetadata(width=400, height=200, format="PNG", color_mode="RGB", orientation=None)
    frame = np.zeros((200, 400, 3), dtype=np.uint8)
    results = adapter.run(frame, meta)

    assert results, "FixtureOcrAdapter must return at least one result"

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="fixture-e2e",
            batch_config=OcrBatchConfig(batch_size=10),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    assert batches
    obs = batches[0].observations
    assert obs, "expected observations in batch"
    for o in obs:
        assert o.observation_type == "ocr_text_mention"
        assert o.case_id == job.case_id
        assert o.evidence_id == job.evidence_id
        assert o.source_locator.bbox_xyxy_normalized is not None
        assert len(o.extracted_entities) == 1
        assert o.extracted_entities[0].text.startswith(FIXTURE_TEXT_PREFIX)


def test_fixture_adapter_to_batch_image_locator_has_no_page_field() -> None:
    """Image OCR locator must not have a page field (pages are Jasraj's domain)."""
    import numpy as np

    job = _make_job()
    adapter = FixtureOcrAdapter()
    meta = ImageMetadata(width=400, height=200, format="PNG", color_mode="RGB", orientation=None)
    frame = np.zeros((200, 400, 3), dtype=np.uint8)
    results = adapter.run(frame, meta)

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="no-page",
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    for batch in batches:
        for obs in batch.observations:
            assert obs.source_locator.page is None, (
                "image OCR source locator must not set 'page' (that's Jasraj's domain)"
            )


def test_progress_units_completed_is_monotonically_increasing() -> None:
    """Progress units_completed increases monotonically across batches."""
    job = _make_job()
    results = [_make_ocr_result(f"T{i}") for i in range(6)]

    batches = list(
        iter_image_ocr_batches(
            results,
            job=job,
            idempotency_key_prefix="mono",
            batch_config=OcrBatchConfig(batch_size=2),
            started_at=FIXED_TIME,
            completed_at=FIXED_TIME,
        )
    )
    units_completed = [b.progress.units_completed for b in batches]
    for i in range(1, len(units_completed)):
        assert units_completed[i] >= units_completed[i - 1], (
            f"units_completed must be non-decreasing: {units_completed}"
        )


def test_step_name_constants_are_distinct() -> None:
    """Image and video-frame OCR step names are distinct strings."""
    assert STEP_NAME_IMAGE_OCR != STEP_NAME_VIDEO_FRAME_OCR
    assert "image" in STEP_NAME_IMAGE_OCR
    assert "video" in STEP_NAME_VIDEO_FRAME_OCR
