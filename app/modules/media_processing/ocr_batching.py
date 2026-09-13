"""OCR micro-batch builder for the media-processing worker (Phase 3 — Gaurav).

Converts ``OcrBoxResult`` lists from ``ocr_adapter.ImageOcrAdapter`` into
``ObservationBatchSubmissionV1`` payloads that the worker submits to Nipun's
``POST /api/v1/internal/worker-jobs/{job_id}/observations`` endpoint.

Architecture rules this module enforces
-----------------------------------------
* **No direct database writes.** All output goes through the existing client
  (``media_processing.client.WorkerApiClient.submit_batch``).
* **Safe provenance only.** ``TransformationProvenanceV1.safe_metadata`` never
  carries object URIs, claim tokens, credentials, raw OCR text dumps, or stack
  traces -- only short, safe bookkeeping values.
* **Bounded micro-batches.** Callers control ``batch_size``; this module yields
  one ``ObservationBatchSubmissionV1`` per chunk, never accumulating all OCR
  output into a single giant request.
* **Deterministic identity.** ``observation_id`` is computed via
  ``provenance.observation_id`` (same input → same UUID), enabling idempotent
  replay.
* **Terminal result carries no observations.** After all batches are submitted
  the caller must submit a terminal ``WorkerResultV1(observations=[])`` -- this
  module provides ``build_terminal_result`` for that step.
* **Video-frame locators are complete.** Every OCR result from a video frame
  requires ``frame_number`` (or ``None`` when unknowable), ``time_start_ms``,
  ``time_end_ms``, and ``bbox_xyxy_normalized`` in the locator.
* **Image locators require at least a bbox.** A standalone-image OCR
  observation with no bbox is rejected before it reaches the server.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ObservationV1
from app.contracts.observation_batch import (
    ObservationBatchProgressV1,
    ObservationBatchSubmissionV1,
    TransformationProvenanceV1,
    TransformationStatus,
)
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.modules.media_processing.models import ExtractedFrame, MediaObservationDraft, VideoMetadata
from app.modules.media_processing.ocr_adapter import (
    OBSERVATION_TYPE_OCR_TEXT,
    OCR_ADAPTER_PREPROCESSING_VERSION,
    OcrBoxResult,
)
from app.modules.media_processing.provenance import (
    build_extractor,
    draft_to_observation,
    is_valid_confidence,
)

#: Mirrors worker.PROCESSOR_NAME_DETECTION -- kept here as a literal to
#: avoid a circular import (worker imports ocr_batching via lazy import).
_PROCESSOR_NAME = "media_detection_v1"
_PROCESSOR_VERSION = "1.0.0"

#: The step name used in every ``TransformationProvenanceV1`` produced here.
#: Distinguishable from structured-processing's ``pdf_text_extraction`` etc.
STEP_NAME_IMAGE_OCR = "image_ocr_text_extraction"
STEP_NAME_VIDEO_FRAME_OCR = "video_frame_ocr_text_extraction"
STEP_NAME_MEDIA_OBSERVATION_SUBMISSION = "media_observation_submission"


# ---------------------------------------------------------------------------
# Batch configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcrBatchConfig:
    """Configuration for OCR micro-batch construction.

    ``batch_size`` controls how many ``OcrBoxResult`` items land in a single
    ``ObservationBatchSubmissionV1``.  Choose a value that keeps individual
    HTTP payloads well under the server's body-size limit while still
    providing meaningful progress granularity.

    ``units_total`` is the total number of OCR results expected across all
    batches for one job.  ``None`` means the total wasn't known ahead of time
    (common for video: the frame count depends on the actual extracted frames).
    """

    batch_size: int = 50
    stage_name: str = "ocr_extraction"
    units_total: int | None = None


# ---------------------------------------------------------------------------
# Per-result observation builder
# ---------------------------------------------------------------------------


def build_image_ocr_observation(
    result: OcrBoxResult,
    *,
    job: WorkerJobV1,
    extractor: Extractor,
    completed_at: datetime,
) -> ObservationV1:
    """Build one ``ObservationV1`` from a standalone-image ``OcrBoxResult``.

    Source locator: ``bbox_xyxy_normalized`` (required) + safe dimension
    attributes.  Does not include ``page`` -- a raster image is not a
    document page (that's Jasraj's domain).
    """
    draft = MediaObservationDraft(
        observation_type=OBSERVATION_TYPE_OCR_TEXT,
        locator=SourceLocator(bbox_xyxy_normalized=result.bbox_xyxy_normalized),
        confidence=result.confidence,
        entity_text=result.text,
        entity_type_hint="ocr_text",
        attributes={
            "ocr_unit": result.ocr_unit,
            "source_image_width": result.source_image_width,
            "source_image_height": result.source_image_height,
            "ocr_engine": result.ocr_engine_name,
            "ocr_language": result.language,
            "preprocessing_version": result.preprocessing_version,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=extractor,
        created_at=completed_at,
    )


def build_video_frame_ocr_observation(
    result: OcrBoxResult,
    *,
    job: WorkerJobV1,
    frame: ExtractedFrame,
    metadata: VideoMetadata,
    extractor: Extractor,
    completed_at: datetime,
) -> ObservationV1:
    """Build one ``ObservationV1`` from a video-frame ``OcrBoxResult``.

    Source locator: ``frame_number``, ``time_start_ms``, ``time_end_ms``,
    ``bbox_xyxy_normalized`` -- all required for a valid video-frame locator.
    ``time_end_ms`` derives from the *frame*'s own ``time_end_ms`` (set by
    ``video/frames.py`` from the inter-frame interval), not from video
    ``duration_ms``, so it is always frame-precise.
    """
    draft = MediaObservationDraft(
        observation_type=OBSERVATION_TYPE_OCR_TEXT,
        locator=SourceLocator(
            frame_number=frame.frame_number,
            time_start_ms=frame.time_start_ms,
            time_end_ms=frame.time_end_ms,
            bbox_xyxy_normalized=result.bbox_xyxy_normalized,
        ),
        confidence=result.confidence,
        entity_text=result.text,
        entity_type_hint="ocr_text",
        attributes={
            "ocr_unit": result.ocr_unit,
            "source_image_width": result.source_image_width,
            "source_image_height": result.source_image_height,
            "media_width": metadata.width,
            "media_height": metadata.height,
            "ocr_engine": result.ocr_engine_name,
            "ocr_language": result.language,
            "preprocessing_version": result.preprocessing_version,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=extractor,
        created_at=completed_at,
    )


# ---------------------------------------------------------------------------
# Transformation provenance builder
# ---------------------------------------------------------------------------


def build_ocr_transformation_provenance(
    *,
    job: WorkerJobV1,
    batch_id: str,
    ordinal: int,
    observations: list[ObservationV1],
    ocr_engine: str,
    ocr_engine_version: str,
    ocr_language: str,
    config_hash: str,
    source_width: int,
    source_height: int,
    step_name: str = STEP_NAME_IMAGE_OCR,
    frame_info: ExtractedFrame | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> TransformationProvenanceV1:
    """Build a ``TransformationProvenanceV1`` for one OCR micro-batch.

    ``safe_metadata`` carries only short, bookkeeping values -- never a URI,
    credential, raw OCR text dump, stack trace, or SQL.  This is validated by
    ``TransformationProvenanceV1``'s own ``_validate_safe_metadata`` rule.

    ``output_observation_ids`` must be a subset of the *same* batch's
    observations (enforced by ``ObservationBatchSubmissionV1``'s validator).
    """
    now = datetime.now(UTC)
    completed_at = completed_at or now
    started_at = started_at or completed_at

    safe_meta: dict[str, JsonValue] = {
        "ocr_engine": ocr_engine,
        "ocr_engine_version": ocr_engine_version,
        "ocr_language": ocr_language,
        "preprocessing_version": OCR_ADAPTER_PREPROCESSING_VERSION,
        "source_width": source_width,
        "source_height": source_height,
        "observation_count": len(observations),
    }
    if frame_info is not None:
        safe_meta["frame_time_start_ms"] = frame_info.time_start_ms
        safe_meta["frame_time_end_ms"] = frame_info.time_end_ms
        if frame_info.frame_number is not None:
            safe_meta["frame_number"] = frame_info.frame_number

    # Compute the stable step version from config_hash + adapter version --
    # the same config always yields the same step_version string.
    step_version = f"{OCR_ADAPTER_PREPROCESSING_VERSION}+{config_hash[:16]}"

    return TransformationProvenanceV1(
        transformation_id=uuid4(),
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        job_id=job.job_id,
        batch_id=batch_id,
        ordinal=ordinal,
        step_name=step_name,
        step_version=step_version,
        config_hash=config_hash,
        model_version=ocr_engine,
        input_locator=(
            SourceLocator(
                frame_number=frame_info.frame_number,
                time_start_ms=frame_info.time_start_ms,
                time_end_ms=frame_info.time_end_ms,
            )
            if frame_info is not None
            else None
        ),
        output_observation_ids=[o.observation_id for o in observations],
        derived_artifact_refs=[],
        status=TransformationStatus.SUCCEEDED,
        started_at=started_at,
        completed_at=completed_at,
        safe_metadata=safe_meta,
    )


# ---------------------------------------------------------------------------
# Micro-batch iterator: image
# ---------------------------------------------------------------------------


def iter_image_ocr_batches(
    results: list[OcrBoxResult],
    job: WorkerJobV1,
    idempotency_key_prefix: str,
    batch_config: OcrBatchConfig | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    mark_final_batch: bool = True,
) -> Iterator[ObservationBatchSubmissionV1]:
    """Yield bounded micro-batches for standalone-image OCR results.

    Each batch carries:
    - ``batch_size`` (or fewer) ``ObservationV1`` objects
    - One ``TransformationProvenanceV1`` whose ``output_observation_ids``
      lists exactly those observations' IDs
    - A ``ObservationBatchProgressV1`` with monotonically increasing
      ``units_completed``

    ``idempotency_key_prefix`` should be unique per ``(job_id, attempt)``
    and is combined with the batch sequence number to produce a per-batch
    idempotency key.  Replaying the exact same sequence of results yields
    the same idempotency keys, enabling safe retry.

    Results with invalid confidence are silently dropped (same policy as
    ``_whole_image_ocr_observations`` in ``worker.py``).
    """
    now = datetime.now(UTC)
    completed_at = completed_at or now
    started_at = started_at or completed_at
    batch_config = batch_config or OcrBatchConfig()

    extractor = build_extractor(
        processor_name=_PROCESSOR_NAME,
        processor_version=_PROCESSOR_VERSION,
        model_version=results[0].ocr_engine_name if len(results) > 0 else "n/a",
    )

    valid_results = [r for r in results if is_valid_confidence(r.confidence)]
    total = len(valid_results)
    units_total = batch_config.units_total if batch_config.units_total is not None else total

    batch_size = batch_config.batch_size
    if total == 0:
        # No valid results: yield one empty batch as a "nothing found" signal.
        chunks: list[list[OcrBoxResult]] = [[]]
    else:
        chunks = [valid_results[i : i + batch_size] for i in range(0, total, batch_size)]

    if not chunks:
        chunks = [[]]

    for seq, chunk in enumerate(chunks):
        batch_id = f"{idempotency_key_prefix}-b{seq}"
        idempotency_key = f"{idempotency_key_prefix}-ik{seq}"

        # Use first result's metadata for provenance (all from same image).
        if chunk:
            first = chunk[0]
            ocr_engine = first.ocr_engine_name
            ocr_engine_version = first.ocr_engine_version
            ocr_language = first.language
            config_hash = first.config_hash
            source_width = first.source_image_width
            source_height = first.source_image_height
        else:
            ocr_engine = "n/a"
            ocr_engine_version = "n/a"
            ocr_language = "n/a"
            config_hash = "n/a"
            source_width = 0
            source_height = 0

        observations = [
            build_image_ocr_observation(r, job=job, extractor=extractor, completed_at=completed_at)
            for r in chunk
        ]

        units_completed = min((seq + 1) * batch_size, total)
        observations_emitted = sum(len(c) for c in chunks[: seq + 1])

        provenance = build_ocr_transformation_provenance(
            job=job,
            batch_id=batch_id,
            ordinal=0,
            observations=observations,
            ocr_engine=ocr_engine,
            ocr_engine_version=ocr_engine_version,
            ocr_language=ocr_language,
            config_hash=config_hash,
            source_width=source_width,
            source_height=source_height,
            step_name=STEP_NAME_IMAGE_OCR,
            started_at=started_at,
            completed_at=completed_at,
        )

        # `run_once` may append a non-OCR media-observation batch after the
        # OCR batches.  In that orchestration path it owns the job-global
        # final marker, so this iterator must not claim the final slot early.
        is_final = mark_final_batch and seq == len(chunks) - 1
        progress = ObservationBatchProgressV1(
            stage=batch_config.stage_name,
            units_total=units_total if not math.isinf(float(units_total)) else None,
            units_completed=units_completed,
            observations_emitted=observations_emitted,
            batch_sequence=seq,
            message_code="OCR_REGIONS_EXTRACTED",
            occurred_at=completed_at,
        )

        yield ObservationBatchSubmissionV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            batch_id=batch_id,
            batch_sequence=seq,
            idempotency_key=idempotency_key,
            observations=observations,
            transformations=[provenance],
            progress=progress,
            submitted_at=completed_at,
            is_final_batch=is_final,
        )


def build_media_observation_batch(
    observations: Sequence[ObservationV1],
    *,
    job: WorkerJobV1,
    batch_sequence: int,
    idempotency_key_prefix: str,
    observations_emitted_before: int,
    completed_at: datetime | None = None,
    is_final: bool,
) -> ObservationBatchSubmissionV1:
    """Wrap the existing metadata/detection/tracking observations in one batch.

    ``process_job`` deliberately remains a pure local-analysis function.  The
    claim-bound worker orchestration owns HTTP submission, and uses this
    helper so *every* successful observation follows the secure micro-batch
    path and the one terminal ``WorkerResultV1`` always has
    ``observations=[]``.  The transformation contains no OCR payload or
    source content: it is only safe bookkeeping for the observations emitted
    by the pre-existing media analysis stages.
    """
    now = completed_at or datetime.now(UTC)
    batch_id = f"{idempotency_key_prefix}-media-b{batch_sequence}"
    observation_types = sorted({observation.observation_type for observation in observations})
    transformation = TransformationProvenanceV1(
        transformation_id=uuid4(),
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        job_id=job.job_id,
        batch_id=batch_id,
        ordinal=0,
        step_name=STEP_NAME_MEDIA_OBSERVATION_SUBMISSION,
        step_version="media_observation_submission_v1",
        config_hash="media_observation_submission_v1",
        model_version="n/a",
        input_locator=None,
        output_observation_ids=[observation.observation_id for observation in observations],
        derived_artifact_refs=[],
        status=TransformationStatus.SUCCEEDED,
        started_at=now,
        completed_at=now,
        safe_metadata={
            "observation_count": len(observations),
            "observation_types": cast(JsonValue, observation_types),
            "processor_name": _PROCESSOR_NAME,
            "processor_version": _PROCESSOR_VERSION,
        },
    )
    return ObservationBatchSubmissionV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id=batch_id,
        batch_sequence=batch_sequence,
        idempotency_key=f"{idempotency_key_prefix}-media-ik{batch_sequence}",
        observations=list(observations),
        transformations=[transformation],
        progress=ObservationBatchProgressV1(
            stage="media_observation_submission",
            units_total=1,
            units_completed=1,
            observations_emitted=observations_emitted_before + len(observations),
            batch_sequence=batch_sequence,
            message_code="MEDIA_OBSERVATIONS_SUBMITTED",
            occurred_at=now,
        ),
        submitted_at=now,
        is_final_batch=is_final,
    )


# ---------------------------------------------------------------------------
# Micro-batch builder: video
# ---------------------------------------------------------------------------


def build_video_frame_ocr_batch(
    results: Sequence[OcrBoxResult],
    *,
    job: WorkerJobV1,
    frame: ExtractedFrame,
    metadata: VideoMetadata,
    batch_id: str,
    batch_sequence: int,
    idempotency_key: str,
    units_completed: int,
    units_total: int | None,
    observations_emitted_before: int,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    is_final: bool = False,
) -> ObservationBatchSubmissionV1:
    """Build one ``ObservationBatchSubmissionV1`` for video-frame OCR results.

    Unlike the image case (which iterates chunks), each call here covers
    exactly one video frame's OCR output.  The caller (the worker's
    video-analysis loop) manages frame-level batching; this function
    produces the structured payload for exactly that frame.

    ``frame`` carries ``frame_number`` (may be ``None``), ``time_start_ms``,
    and ``time_end_ms`` -- all required fields for a valid video-frame
    source locator.
    """
    now = datetime.now(UTC)
    completed_at = completed_at or now
    started_at = started_at or completed_at

    valid_results = [r for r in results if is_valid_confidence(r.confidence)]
    extractor = build_extractor(
        processor_name=_PROCESSOR_NAME,
        processor_version=_PROCESSOR_VERSION,
        model_version=valid_results[0].ocr_engine_name if valid_results else "n/a",
    )

    observations = [
        build_video_frame_ocr_observation(
            r,
            job=job,
            frame=frame,
            metadata=metadata,
            extractor=extractor,
            completed_at=completed_at,
        )
        for r in valid_results
    ]

    if valid_results:
        first = valid_results[0]
        ocr_engine = first.ocr_engine_name
        ocr_engine_version = first.ocr_engine_version
        ocr_language = first.language
        config_hash = first.config_hash
        source_width = first.source_image_width
        source_height = first.source_image_height
    else:
        ocr_engine = "n/a"
        ocr_engine_version = "n/a"
        ocr_language = "n/a"
        config_hash = "n/a"
        source_width = metadata.width
        source_height = metadata.height

    provenance = build_ocr_transformation_provenance(
        job=job,
        batch_id=batch_id,
        ordinal=0,
        observations=observations,
        ocr_engine=ocr_engine,
        ocr_engine_version=ocr_engine_version,
        ocr_language=ocr_language,
        config_hash=config_hash,
        source_width=source_width,
        source_height=source_height,
        step_name=STEP_NAME_VIDEO_FRAME_OCR,
        frame_info=frame,
        started_at=started_at,
        completed_at=completed_at,
    )

    progress = ObservationBatchProgressV1(
        stage="video_frame_ocr",
        units_total=units_total,
        units_completed=units_completed,
        observations_emitted=observations_emitted_before + len(observations),
        batch_sequence=batch_sequence,
        message_code="FRAME_OCR_EXTRACTED",
        occurred_at=completed_at,
    )

    return ObservationBatchSubmissionV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        batch_id=batch_id,
        batch_sequence=batch_sequence,
        idempotency_key=idempotency_key,
        observations=observations,
        transformations=[provenance] if (observations or not is_final) else [],
        progress=progress,
        submitted_at=completed_at,
        is_final_batch=is_final,
    )


# ---------------------------------------------------------------------------
# Terminal result builder
# ---------------------------------------------------------------------------


def build_terminal_result(
    *,
    job_id: UUID,
    case_id: UUID,
    evidence_id: UUID,
    completed_at: datetime | None = None,
) -> WorkerResultV1:
    """A terminal ``WorkerResultV1`` with ``observations=[]``.

    After all OCR micro-batches have been submitted via the batch endpoint,
    the worker must still submit one terminal result to transition the job
    out of ``running`` state.  This result carries no observations (they
    were already delivered) -- ``observation_count`` in the server's job
    view reflects the batch-delivered ones.

    Per requirement §6: ``observations`` must be empty here.
    """
    now = completed_at or datetime.now(UTC)
    return WorkerResultV1(
        job_id=job_id,
        case_id=case_id,
        evidence_id=evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=None,
        completed_at=now,
    )


def build_terminal_result_failed(
    *,
    job_id: UUID,
    case_id: UUID,
    evidence_id: UUID,
    error_code: str,
    error_message: str,
    retryable: bool = False,
    completed_at: datetime | None = None,
) -> WorkerResultV1:
    """A terminal ``WorkerResultV1`` for a failed OCR job."""
    now = completed_at or datetime.now(UTC)
    return WorkerResultV1(
        job_id=job_id,
        case_id=case_id,
        evidence_id=evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=WorkerError(code=error_code, message=error_message, retryable=retryable),
        completed_at=now,
    )


__all__ = [
    "STEP_NAME_IMAGE_OCR",
    "STEP_NAME_MEDIA_OBSERVATION_SUBMISSION",
    "STEP_NAME_VIDEO_FRAME_OCR",
    "OcrBatchConfig",
    "build_image_ocr_observation",
    "build_media_observation_batch",
    "build_video_frame_ocr_observation",
    "build_ocr_transformation_provenance",
    "build_terminal_result",
    "build_terminal_result_failed",
    "build_video_frame_ocr_batch",
    "iter_image_ocr_batches",
]
