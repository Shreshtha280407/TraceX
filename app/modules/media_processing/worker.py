"""The media-processing worker entry point: `WorkerJobV1` in, `WorkerResultV1` out.

`process_job` never touches PostgreSQL, Neo4j, Redis, or MinIO -- bytes come
in through a `SourceResolver`, and everything it returns is either a
canonical `ObservationV1` or a safe, structured `WorkerError`. Detection,
tracking, and OCR analysis components are always explicit keyword
arguments with no default -- there is no real implementation in this
phase, and a fake implementation must never be silently substituted for
one (see `analysis/fake_*.py`).

Two kinds of failure are handled differently, deliberately: a job-level
problem (unsupported content type, over-limit media, an unusable probe)
fails the whole job via `ProcessingError`; a single malformed detector/
tracker/OCR output item (an out-of-range confidence, invalid bounding-box
geometry) is skipped -- CLAUDE.md and this module's spec both require that
one bad model output must never discard an otherwise-good result, nor be
silently "fixed" into range.

`run_once`/`main` (bottom of this file) are the Phase 2 addition: a
one-shot CLI runner --

    uv run python -m app.modules.media_processing.worker --once

-- that claims one compatible job through Nipun's internal worker API
(`client.py`), resolves its evidence via the injected
`input_resolver.WorkerInputResolver`, verifies its SHA-256 *before* any
decode is attempted, shims the minimal `EvidenceRecordV1` `process_job`
expects (see `_shim_evidence_record`, mirroring
`structured_processing.worker`'s identical precedent), calls `process_job`
completely unchanged, and submits the result. No daemon, polling loop, or
scheduler -- see `docs/architecture/media-processing-worker.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from app.contracts.common import Extractor, SourceLocator
from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
    SourceType,
)
from app.contracts.observation import ObservationV1
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.core.config import Settings, get_settings
from app.modules.media_processing.analysis.interfaces import (
    ObjectDetection,
    ObjectDetector,
    ObjectTracker,
    TextRecognizer,
    TrackSegment,
)
from app.modules.media_processing.client import WorkerApiClient
from app.modules.media_processing.errors import (
    ErrorCode,
    InputResolutionUnavailableError,
    ProcessingError,
    WorkerApiError,
    WorkerAuthenticationError,
)
from app.modules.media_processing.image.decoder import decode_image
from app.modules.media_processing.image.geometry import PixelBoundingBox, to_normalized
from app.modules.media_processing.input_resolver import (
    LiveInputResolver,
    ResolvedMediaInput,
    WorkerInputResolver,
)
from app.modules.media_processing.limits import (
    DEFAULT_MEDIA_LIMITS,
    MediaLimits,
    check_image_limits,
    check_input_size,
    check_video_limits,
)
from app.modules.media_processing.models import (
    ExtractedFrame,
    Frame,
    ImageMetadata,
    MediaObservationDraft,
    SamplingRequest,
    SamplingStrategy,
    VideoMetadata,
)
from app.modules.media_processing.performance import Stopwatch
from app.modules.media_processing.provenance import (
    CONFIDENCE_METADATA_PROBED,
    OBSERVATION_ANONYMOUS_TRACK_SEGMENT,
    OBSERVATION_MEDIA_METADATA,
    OBSERVATION_OBJECT_DETECTION,
    OBSERVATION_OCR_TEXT_MENTION,
    OBSERVATION_TEXT_REGION_DETECTION,
    build_extractor,
    draft_to_observation,
    is_valid_confidence,
)
from app.modules.media_processing.source import (
    MediaKind,
    SourceResolver,
    StaticBytesResolver,
    classify_media,
    is_video,
    temporary_media_file,
)
from app.modules.media_processing.video.frames import extract_frames
from app.modules.media_processing.video.probe import probe_video
from app.modules.media_processing.video.sampling import build_sample_plan

logger = structlog.get_logger(__name__)

PROCESSOR_NAME_METADATA = "media_metadata_v1"
PROCESSOR_NAME_DETECTION = "media_detection_v1"
PROCESSOR_VERSION = "1.0.0"

_TEXT_REGION_LABEL = "text_region"

#: Default sampling policy when a caller doesn't specify one: one frame per
#: second, up to the configured maximum. See
#: docs/architecture/media-processing-v1.md for the documented policy.
_DEFAULT_SAMPLING = SamplingRequest(
    strategy=SamplingStrategy.UNIFORM_INTERVAL,
    interval_ms=1000,
    max_frames=DEFAULT_MEDIA_LIMITS.max_sampled_frames,
)


def process_job(
    job: WorkerJobV1,
    evidence: EvidenceRecordV1,
    resolver: SourceResolver,
    *,
    limits: MediaLimits = DEFAULT_MEDIA_LIMITS,
    sampling: SamplingRequest | None = None,
    detector: ObjectDetector | None = None,
    tracker: ObjectTracker | None = None,
    ocr: TextRecognizer | None = None,
    stopwatch: Stopwatch | None = None,
) -> WorkerResultV1:
    """Process one video/image evidence source into a `WorkerResultV1`.

    `job.processor_name` selects the pipeline: `"media_metadata_v1"` runs
    metadata-only (no analysis component required); `"media_detection_v1"`
    additionally requires `detector` to be provided, and optionally uses
    `tracker`/`ocr` if given. `stopwatch`, if provided, is populated with
    measured per-stage timings (see `performance.py`); it is a side-channel
    only and never affects the returned `WorkerResultV1`.
    """
    completed_at = datetime.now(UTC)
    sw = stopwatch or Stopwatch()
    try:
        with sw.total():
            observations = _process(
                job, evidence, resolver, limits, sampling, detector, tracker, ocr, sw, completed_at
            )
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.SUCCEEDED,
            observations=observations,
            derived_artifacts=[],
            checkpoint=None,
            error=None,
            completed_at=completed_at,
        )
    except ProcessingError as exc:
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.FAILED,
            observations=[],
            derived_artifacts=[],
            checkpoint=None,
            error=WorkerError(code=exc.code, message=exc.message, retryable=exc.retryable),
            completed_at=completed_at,
        )


def _process(
    job: WorkerJobV1,
    evidence: EvidenceRecordV1,
    resolver: SourceResolver,
    limits: MediaLimits,
    sampling: SamplingRequest | None,
    detector: ObjectDetector | None,
    tracker: ObjectTracker | None,
    ocr: TextRecognizer | None,
    sw: Stopwatch,
    completed_at: datetime,
) -> list[ObservationV1]:
    kind = classify_media(evidence.content_type, evidence.original_filename)
    _check_source_type(kind, evidence.source_type)

    run_analysis = job.processor_name == PROCESSOR_NAME_DETECTION
    if run_analysis and detector is None:
        raise ProcessingError(
            ErrorCode.ANALYSIS_NOT_CONFIGURED,
            f"processor '{job.processor_name}' requires a detector to be configured",
        )
    if job.processor_name not in (PROCESSOR_NAME_METADATA, PROCESSOR_NAME_DETECTION):
        raise ProcessingError(
            ErrorCode.ANALYSIS_NOT_CONFIGURED, f"unknown media processor '{job.processor_name}'"
        )

    data = resolver.read_bytes(job.input_object_uri)
    check_input_size(len(data), limits)

    if is_video(kind):
        with temporary_media_file(data, suffix=".bin") as path:
            with sw.stage("probe_ms"):
                video_metadata = probe_video(path, limits=limits)
            check_video_limits(video_metadata, limits)
            observations = [_video_metadata_observation(job, video_metadata, completed_at)]
            if run_analysis and detector is not None:
                observations.extend(
                    _process_video_analysis(
                        job,
                        path,
                        video_metadata,
                        limits,
                        sampling or _DEFAULT_SAMPLING,
                        detector,
                        tracker,
                        ocr,
                        sw,
                        completed_at,
                    )
                )
            return observations

    with sw.stage("probe_ms"):
        image, image_metadata = decode_image(data, limits=limits)
    check_image_limits(image_metadata, limits)
    observations = [_image_metadata_observation(job, image_metadata, completed_at)]
    if run_analysis and detector is not None:
        with sw.stage("analysis_ms"):
            detections = [d for d in detector.detect(image) if is_valid_confidence(d.confidence)]
        with sw.stage("observation_construction_ms"):
            for detection in detections:
                observation = _detection_observation_image(
                    job, detection, image_metadata, completed_at
                )
                if observation is not None:
                    observations.append(observation)
                if detection.label == _TEXT_REGION_LABEL and ocr is not None:
                    ocr_observation = _ocr_observation_image(
                        job, detection, image, image_metadata, ocr, completed_at
                    )
                    if ocr_observation is not None:
                        observations.append(ocr_observation)
    return observations


def _check_source_type(kind: MediaKind, source_type: SourceType) -> None:
    expected = SourceType.VIDEO if is_video(kind) else SourceType.IMAGE
    if source_type != expected:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_CONTENT_TYPE,
            f"evidence source_type '{source_type.value}' does not match its media content type",
        )


def _metadata_extractor() -> Extractor:
    return build_extractor(
        processor_name=PROCESSOR_NAME_METADATA,
        processor_version=PROCESSOR_VERSION,
        model_version="n/a",
    )


def _analysis_extractor(model_interface_version: str) -> Extractor:
    return build_extractor(
        processor_name=PROCESSOR_NAME_DETECTION,
        processor_version=PROCESSOR_VERSION,
        model_version=model_interface_version,
    )


def _model_interface_version(attributes: dict[str, JsonValue]) -> str:
    value = attributes.get("model_interface_version")
    return value if isinstance(value, str) else "unknown"


def _video_metadata_observation(
    job: WorkerJobV1, metadata: VideoMetadata, completed_at: datetime
) -> ObservationV1:
    draft = MediaObservationDraft(
        observation_type=OBSERVATION_MEDIA_METADATA,
        locator=SourceLocator(time_start_ms=0),
        confidence=CONFIDENCE_METADATA_PROBED,
        attributes={
            "media_width": metadata.width,
            "media_height": metadata.height,
            "container_format": metadata.container_format,
            "duration_ms": metadata.duration_ms,
            "frame_rate": metadata.frame_rate,
            "frame_count": metadata.frame_count,
            "video_codec": metadata.video_codec,
            "has_audio": metadata.has_audio,
            "rotation_degrees": metadata.rotation_degrees,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_metadata_extractor(),
        created_at=completed_at,
    )


def _image_metadata_observation(
    job: WorkerJobV1, metadata: ImageMetadata, completed_at: datetime
) -> ObservationV1:
    draft = MediaObservationDraft(
        observation_type=OBSERVATION_MEDIA_METADATA,
        locator=SourceLocator(time_start_ms=0),
        confidence=CONFIDENCE_METADATA_PROBED,
        attributes={
            "media_width": metadata.width,
            "media_height": metadata.height,
            "format": metadata.format,
            "color_mode": metadata.color_mode,
            "orientation": metadata.orientation,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_metadata_extractor(),
        created_at=completed_at,
    )


def _crop(image: Frame, box: PixelBoundingBox) -> Frame:
    x_min, y_min = max(int(box.x_min), 0), max(int(box.y_min), 0)
    x_max, y_max = min(int(box.x_max), image.shape[1]), min(int(box.y_max), image.shape[0])
    return image[y_min:y_max, x_min:x_max]


def _detection_observation_video(
    job: WorkerJobV1,
    detection: ObjectDetection,
    frame: ExtractedFrame,
    metadata: VideoMetadata,
    sampling: SamplingRequest,
    completed_at: datetime,
) -> ObservationV1 | None:
    try:
        normalized = to_normalized(
            detection.box, image_width=metadata.width, image_height=metadata.height
        )
    except ProcessingError:
        return None
    observation_type = (
        OBSERVATION_TEXT_REGION_DETECTION
        if detection.label == _TEXT_REGION_LABEL
        else OBSERVATION_OBJECT_DETECTION
    )
    draft = MediaObservationDraft(
        observation_type=observation_type,
        locator=SourceLocator(
            frame_number=frame.frame_number,
            time_start_ms=frame.time_start_ms,
            time_end_ms=frame.time_end_ms,
            bbox_xyxy_normalized=normalized,
        ),
        confidence=detection.confidence,
        attributes={
            "detected_label": detection.label,
            "media_width": metadata.width,
            "media_height": metadata.height,
            "sampling_strategy": sampling.strategy.value,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_analysis_extractor(_model_interface_version(dict(detection.attributes))),
        created_at=completed_at,
    )


def _detection_observation_image(
    job: WorkerJobV1, detection: ObjectDetection, metadata: ImageMetadata, completed_at: datetime
) -> ObservationV1 | None:
    try:
        normalized = to_normalized(
            detection.box, image_width=metadata.width, image_height=metadata.height
        )
    except ProcessingError:
        return None
    observation_type = (
        OBSERVATION_TEXT_REGION_DETECTION
        if detection.label == _TEXT_REGION_LABEL
        else OBSERVATION_OBJECT_DETECTION
    )
    draft = MediaObservationDraft(
        observation_type=observation_type,
        locator=SourceLocator(bbox_xyxy_normalized=normalized),
        confidence=detection.confidence,
        attributes={
            "detected_label": detection.label,
            "media_width": metadata.width,
            "media_height": metadata.height,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_analysis_extractor(_model_interface_version(dict(detection.attributes))),
        created_at=completed_at,
    )


def _ocr_observation_image(
    job: WorkerJobV1,
    detection: ObjectDetection,
    image: Frame,
    metadata: ImageMetadata,
    ocr: TextRecognizer,
    completed_at: datetime,
) -> ObservationV1 | None:
    recognized = ocr.recognize(_crop(image, detection.box))
    if recognized is None or not is_valid_confidence(recognized.confidence):
        return None
    try:
        normalized = to_normalized(
            detection.box, image_width=metadata.width, image_height=metadata.height
        )
    except ProcessingError:
        return None
    draft = MediaObservationDraft(
        observation_type=OBSERVATION_OCR_TEXT_MENTION,
        locator=SourceLocator(bbox_xyxy_normalized=normalized),
        confidence=recognized.confidence,
        entity_text=recognized.text,
        entity_type_hint="ocr_text",
        attributes={"media_width": metadata.width, "media_height": metadata.height},
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_analysis_extractor(_model_interface_version(dict(recognized.attributes))),
        created_at=completed_at,
    )


def _ocr_observation_video(
    job: WorkerJobV1,
    detection: ObjectDetection,
    frame: ExtractedFrame,
    metadata: VideoMetadata,
    sampling: SamplingRequest,
    ocr: TextRecognizer,
    completed_at: datetime,
) -> ObservationV1 | None:
    recognized = ocr.recognize(_crop(frame.image, detection.box))
    if recognized is None or not is_valid_confidence(recognized.confidence):
        return None
    try:
        normalized = to_normalized(
            detection.box, image_width=metadata.width, image_height=metadata.height
        )
    except ProcessingError:
        return None
    draft = MediaObservationDraft(
        observation_type=OBSERVATION_OCR_TEXT_MENTION,
        locator=SourceLocator(
            frame_number=frame.frame_number,
            time_start_ms=frame.time_start_ms,
            time_end_ms=frame.time_end_ms,
            bbox_xyxy_normalized=normalized,
        ),
        confidence=recognized.confidence,
        entity_text=recognized.text,
        entity_type_hint="ocr_text",
        attributes={
            "media_width": metadata.width,
            "media_height": metadata.height,
            "sampling_strategy": sampling.strategy.value,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_analysis_extractor(_model_interface_version(dict(recognized.attributes))),
        created_at=completed_at,
    )


def _track_observation(
    job: WorkerJobV1,
    segment: TrackSegment,
    metadata: VideoMetadata,
    sampling: SamplingRequest,
    completed_at: datetime,
) -> ObservationV1:
    track_boxes: list[JsonValue] = []
    for time_ms, box in sorted(segment.boxes_by_time_ms.items()):
        try:
            normalized = to_normalized(
                box, image_width=metadata.width, image_height=metadata.height
            )
        except ProcessingError:
            continue
        track_boxes.append(
            {"time_ms": time_ms, "bbox_xyxy_normalized": normalized.model_dump(mode="json")}
        )

    draft = MediaObservationDraft(
        observation_type=OBSERVATION_ANONYMOUS_TRACK_SEGMENT,
        locator=SourceLocator(time_start_ms=segment.start_time_ms, time_end_ms=segment.end_time_ms),
        confidence=segment.quality,
        discriminator=segment.local_track_id,
        attributes={
            "local_track_id": segment.local_track_id,
            "detected_label": segment.label,
            "media_width": metadata.width,
            "media_height": metadata.height,
            "sampling_strategy": sampling.strategy.value,
            "track_boxes": track_boxes,
        },
    )
    return draft_to_observation(
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        draft=draft,
        extractor=_analysis_extractor(_model_interface_version(dict(segment.attributes))),
        created_at=completed_at,
    )


def _process_video_analysis(
    job: WorkerJobV1,
    path: Path,
    metadata: VideoMetadata,
    limits: MediaLimits,
    sampling: SamplingRequest,
    detector: ObjectDetector,
    tracker: ObjectTracker | None,
    ocr: TextRecognizer | None,
    sw: Stopwatch,
    completed_at: datetime,
) -> list[ObservationV1]:
    with sw.stage("sampling_plan_ms"):
        plan = build_sample_plan(metadata, sampling)
    with sw.stage("frame_extraction_ms"):
        frames, frames_failed = extract_frames(path, metadata, plan, limits=limits)
    sw.timings.frames_requested += len(plan)
    sw.timings.frames_extracted += len(frames)
    sw.timings.frames_failed += frames_failed

    detections_by_time: dict[int, list[ObjectDetection]] = {}
    with sw.stage("analysis_ms"):
        for frame in frames:
            detections_by_time[frame.time_start_ms] = [
                d for d in detector.detect(frame.image) if is_valid_confidence(d.confidence)
            ]

    observations: list[ObservationV1] = []
    with sw.stage("observation_construction_ms"):
        for frame in frames:
            for detection in detections_by_time.get(frame.time_start_ms, []):
                observation = _detection_observation_video(
                    job, detection, frame, metadata, sampling, completed_at
                )
                if observation is not None:
                    observations.append(observation)
                if detection.label == _TEXT_REGION_LABEL and ocr is not None:
                    ocr_observation = _ocr_observation_video(
                        job, detection, frame, metadata, sampling, ocr, completed_at
                    )
                    if ocr_observation is not None:
                        observations.append(ocr_observation)

        if tracker is not None:
            for segment in tracker.track(detections_by_time):
                if is_valid_confidence(segment.quality):
                    observations.append(
                        _track_observation(job, segment, metadata, sampling, completed_at)
                    )

    return observations


#: Every `(processor_name, processor_version)` this worker's live claim loop
#: tries. Deliberately **only** `media_metadata_v1`: `media_detection_v1`
#: requires a real `ObjectDetector` to be injected, and no real local
#: detector is approved/available in this phase (see
#: `analysis/interfaces.py`, `analysis/fake_*.py`, and
#: `docs/qa/known-limitations.md`) -- `evidence_lifecycle/routing.py` never
#: routes a real upload to `media_detection_v1` either, so trying to claim
#: it here would only ever return "no work available." `process_job` itself
#: still supports `media_detection_v1` fully (for direct/test invocation,
#: and for a future phase that wires in a real detector); this CLI simply
#: doesn't claim jobs for it yet.
SUPPORTED_PROCESSORS: tuple[tuple[str, str], ...] = ((PROCESSOR_NAME_METADATA, PROCESSOR_VERSION),)

#: A safe, non-secret checkpoint recorded on a `DEFERRED` result when the
#: claimed job's evidence couldn't be resolved because the input-access
#: endpoint isn't reachable -- mirrors `communication_processing.worker`'s
#: identical addition: an honest, expected "not yet possible" outcome,
#: never a fabricated failure or a crash.
CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE = "input_resolution_unavailable"

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunOnceOutcome:
    """What one `run_once` call actually did -- for the CLI's exit behavior and for tests."""

    claimed: bool
    job_id: UUID | None
    result_status: str | None
    deferred_reason: str | None = None


def run_once(
    *,
    client: WorkerApiClient,
    input_resolver: WorkerInputResolver,
    clock: Clock = _default_clock,
    processors: Sequence[tuple[str, str]] = SUPPORTED_PROCESSORS,
) -> RunOnceOutcome:
    """Claim at most one job, process it, submit its result, and return what happened.

    Tries each supported `(processor_name, processor_version)` in turn
    until one yields a job, or returns `claimed=False` once all are
    exhausted -- a normal, successful "no work" outcome, never an error.
    Never submits `queued`/`running` as a result: `process_job` only ever
    returns a terminal `WorkerResultV1`, and the input-resolution-gap and
    integrity-mismatch paths below both submit a terminal result too,
    never fabricating `SUCCEEDED`.
    """
    run_id = str(uuid4())
    structlog.contextvars.bind_contextvars(run_id=run_id)
    try:
        logger.info("worker.run_once.started")
        claim_result = None
        for processor_name, processor_version in processors:
            claim_result = client.claim(
                processor_name=processor_name, processor_version=processor_version
            )
            if claim_result.job is not None:
                break

        if claim_result is None or claim_result.job is None:
            logger.info("worker.run_once.no_job_available")
            return RunOnceOutcome(claimed=False, job_id=None, result_status=None)

        job = claim_result.job
        claim_token = claim_result.claim_token
        if claim_token is None:  # pragma: no cover - defensive: API always pairs job+token
            raise WorkerApiError("internal worker API returned a job without a claim token")

        structlog.contextvars.bind_contextvars(job_id=str(job.job_id))
        logger.info("worker.run_once.claimed", processor_name=job.processor_name)

        try:
            resolved = input_resolver.resolve(job, claim_token=claim_token)
        except InputResolutionUnavailableError as exc:
            logger.warning("worker.run_once.input_unavailable")
            deferred = _deferred_result_for_missing_input(job, clock())
            client.submit_result(job_id=job.job_id, claim_token=claim_token, result=deferred)
            return RunOnceOutcome(
                claimed=True,
                job_id=job.job_id,
                result_status=deferred.status.value,
                deferred_reason=str(exc),
            )

        if resolved.expected_sha256 is not None:
            actual_sha256 = hashlib.sha256(resolved.data).hexdigest()
            if actual_sha256 != resolved.expected_sha256:
                logger.error("worker.run_once.integrity_mismatch")
                mismatch = _failed_result_for_integrity_mismatch(job, clock())
                ack = client.submit_result(
                    job_id=job.job_id, claim_token=claim_token, result=mismatch
                )
                return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)

        evidence = _shim_evidence_record(job, resolved)
        result = process_job(job, evidence, StaticBytesResolver(payload=resolved.data))
        ack = client.submit_result(job_id=job.job_id, claim_token=claim_token, result=result)
        logger.info(
            "worker.run_once.submitted",
            status=ack.status,
            observation_count=ack.observation_count,
        )
        return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "job_id")


def _shim_evidence_record(job: WorkerJobV1, resolved: ResolvedMediaInput) -> EvidenceRecordV1:
    """Reconstruct the minimal `EvidenceRecordV1` that `process_job` actually reads.

    `process_job` reads only `.content_type`/`.original_filename`/
    `.source_type` from this object (see `_process`) -- every other field
    here is either a real, already-known value (`case_id`/`evidence_id`
    from the job; `object_uri` = `job.input_object_uri`; `sha256` computed
    from the bytes actually resolved; `uploaded_at` = `job.requested_at`)
    or an unused structural placeholder Pydantic requires but `process_job`
    never reads (`classification`, `uploaded_by`, `processing_status`,
    `parser_profile`) -- called out explicitly here so a future reader
    never mistakes a placeholder for real evidence metadata a worker
    legitimately has no authenticated way to obtain (see
    `input_resolver.py`'s module docstring). Mirrors
    `structured_processing.worker._shim_evidence_record` exactly -- the
    same established pattern for the same problem.
    """
    return EvidenceRecordV1(
        evidence_id=job.evidence_id,
        case_id=job.case_id,
        source_type=job.source_type,
        original_filename=resolved.original_filename,
        content_type=resolved.content_type,
        object_uri=job.input_object_uri,
        sha256=hashlib.sha256(resolved.data).hexdigest(),
        classification=EvidenceClassification.UNCLASSIFIED,  # placeholder -- unused by process_job
        uploaded_by="unknown",  # placeholder -- unused by process_job
        uploaded_at=job.requested_at,
        parser_profile=None,
        processing_status=EvidenceProcessingStatus.QUEUED,  # placeholder -- unused by process_job
    )


def _deferred_result_for_missing_input(job: WorkerJobV1, completed_at: datetime) -> WorkerResultV1:
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.DEFERRED,
        observations=[],
        derived_artifacts=[],
        checkpoint=CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE,
        error=None,
        completed_at=completed_at,
    )


def _failed_result_for_integrity_mismatch(
    job: WorkerJobV1, completed_at: datetime
) -> WorkerResultV1:
    """A genuine data-integrity problem (not "not yet possible") -- `FAILED`, not `DEFERRED`.

    `retryable=True`: a fresh claim/re-stream could plausibly succeed if
    the mismatch was caused by a one-off transport issue rather than a
    persistently corrupt stored object.
    """
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=WorkerError(
            code="evidence_integrity_mismatch",
            message="resolved evidence bytes do not match the expected SHA-256",
            retryable=True,
        ),
        completed_at=completed_at,
    )


def _configure_logging() -> None:
    """Structured JSON logging to stdout, mirroring `app.main`'s configuration.

    Deliberately duplicated rather than imported from `app.main`: importing
    it would construct the full FastAPI application (every router, every
    other module's module-level dependency wiring) just to log one line
    from a one-shot CLI script.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _build_client(settings: Settings) -> WorkerApiClient:
    token = settings.worker_token
    if token is None:
        raise WorkerAuthenticationError(
            "WORKER_TOKEN is not configured for this worker process; see "
            "docs/architecture/worker-identity-and-security.md"
        )
    return WorkerApiClient(
        base_url=settings.worker_api_base_url, worker_token=token.get_secret_value()
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: `uv run python -m app.modules.media_processing.worker --once`."""
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.media_processing.worker",
        description=(
            "Claim and process at most one compatible media-processing job, then exit. "
            "No daemon or polling mode exists in this phase."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Run exactly one claim-process-submit cycle, then exit.",
    )
    parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()
    try:
        client = _build_client(settings)
    except WorkerAuthenticationError as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1

    try:
        outcome = run_once(client=client, input_resolver=LiveInputResolver(client))
    except (WorkerAuthenticationError, WorkerApiError, InputResolutionUnavailableError) as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1
    finally:
        client.close()

    logger.info(
        "worker.cli.done",
        job_id=str(outcome.job_id) if outcome.job_id else None,
        status=outcome.result_status or "no_job_available",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE",
    "PROCESSOR_NAME_DETECTION",
    "PROCESSOR_NAME_METADATA",
    "PROCESSOR_VERSION",
    "SUPPORTED_PROCESSORS",
    "RunOnceOutcome",
    "main",
    "process_job",
    "run_once",
]
