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
import signal
import sys
import threading
import typing
from collections.abc import Callable, Iterator, Sequence
from collections.abc import Callable as TypingCallable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
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
    TrackSegment,
)
from app.modules.media_processing.analysis.iou_tracker import IoUTracker
from app.modules.media_processing.analysis.onnx_detector import DetectorConfig, OnnxObjectDetector
from app.modules.media_processing.client import WorkerApiClient
from app.modules.media_processing.errors import (
    ErrorCode,
    InputResolutionUnavailableError,
    ModelAssetError,
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
from app.modules.media_processing.ocr_adapter import ImageOcrAdapter, OcrAdapterConfig
from app.modules.media_processing.ocr_batching import (
    OcrBatchConfig,
    build_media_observation_batch,
    build_terminal_result,
    build_terminal_result_failed,
    build_video_frame_ocr_batch,
    iter_image_ocr_batches,
)
from app.modules.media_processing.performance import Stopwatch
from app.modules.media_processing.phase4 import DEEP_PROFILE, RAPID_PROFILE, ProcessingProfile
from app.modules.media_processing.provenance import (
    CONFIDENCE_METADATA_PROBED,
    OBSERVATION_ANONYMOUS_TRACK_SEGMENT,
    OBSERVATION_MEDIA_METADATA,
    OBSERVATION_OBJECT_DETECTION,
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

OcrBatchCallback = TypingCallable[
    [ExtractedFrame | None, Frame, ImageMetadata | VideoMetadata], None
]

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
    ocr_adapter: ImageOcrAdapter | None = None,
    submit_ocr_batch: OcrBatchCallback | None = None,
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
                job,
                evidence,
                resolver,
                limits,
                sampling,
                detector,
                tracker,
                ocr_adapter,
                sw,
                completed_at,
                submit_ocr_batch,
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
    ocr_adapter: ImageOcrAdapter | None,
    sw: Stopwatch,
    completed_at: datetime,
    submit_ocr_batch: OcrBatchCallback | None = None,
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
                        ocr_adapter,
                        sw,
                        completed_at,
                        submit_ocr_batch,
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

        if ocr_adapter is not None and submit_ocr_batch is not None:
            submit_ocr_batch(None, image, image_metadata)
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
    ocr_adapter: ImageOcrAdapter | None,
    sw: Stopwatch,
    completed_at: datetime,
    submit_ocr_batch: OcrBatchCallback | None = None,
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

        if tracker is not None:
            for segment in tracker.track(detections_by_time):
                if is_valid_confidence(segment.quality):
                    observations.append(
                        _track_observation(job, segment, metadata, sampling, completed_at)
                    )

        if ocr_adapter is not None and submit_ocr_batch is not None:
            for frame in frames:
                submit_ocr_batch(frame, frame.image, metadata)

    return observations


#: Every `(processor_name, processor_version)` this worker's live claim loop
#: tries, in order, when real analysis components are available.
#: `evidence_lifecycle/routing.py` now routes real IMAGE/VIDEO uploads to
#: `media_detection_v1` (see `docs/architecture/phase-2-decisions.md`'s
#: "Real local media inference closeout" for why) -- tried first so a real
#: upload is claimed as a detection job, matching how it was routed.
#: `media_metadata_v1` is tried second: no longer reachable via a real
#: upload (routing sends every IMAGE/VIDEO source type to
#: `media_detection_v1` now), but still fully supported for a directly
#: constructed/legacy job. See `_effective_processors`: when no real
#: detector could be loaded (`_build_analysis_components`), `main`/`run_loop`
#: use only the `media_metadata_v1` entry instead of this full tuple, so a
#: worker instance with no model asset bootstrapped never claims -- and
#: then fails -- a detection job another, properly-configured instance
#: could have handled.
SUPPORTED_PROCESSORS: tuple[tuple[str, str], ...] = (
    (PROCESSOR_NAME_DETECTION, PROCESSOR_VERSION),
    (PROCESSOR_NAME_METADATA, PROCESSOR_VERSION),
)

#: The claim set a worker instance falls back to when no real detector
#: could be loaded -- see `SUPPORTED_PROCESSORS`'s docstring.
_METADATA_ONLY_PROCESSORS: tuple[tuple[str, str], ...] = (
    (PROCESSOR_NAME_METADATA, PROCESSOR_VERSION),
)


def _effective_processors(detector: ObjectDetector | None) -> tuple[tuple[str, str], ...]:
    return SUPPORTED_PROCESSORS if detector is not None else _METADATA_ONLY_PROCESSORS


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


@contextmanager
def _lease_heartbeat(
    client: WorkerApiClient, job_id: UUID, claim_token: str, interval_seconds: float
) -> Iterator[None]:
    """Renew this job's lease on a background thread every `interval_seconds`
    while the wrapped block (real analysis -- sampling, detection, OCR) runs.

    A missed or failed renewal is logged and swallowed, never raised into
    the foreground work already in progress -- if the lease genuinely
    expires despite best-effort renewal, the eventual `submit_result` call
    fails loudly and honestly on its own (a reclaimed job's claim token no
    longer matches), which is the correct outcome; a heartbeat's job is to
    make that the rare case, not to guarantee it can never happen. The
    thread is a daemon (never blocks process exit) and is always signaled
    to stop and joined (bounded wait) before this context manager returns,
    whether the wrapped block succeeded or raised.
    """
    stop = threading.Event()

    def _renew_periodically() -> None:
        while not stop.wait(interval_seconds):
            try:
                client.renew(job_id, claim_token=claim_token)
            except (WorkerApiError, WorkerAuthenticationError) as exc:
                logger.warning("worker.lease.renew_failed", reason=str(exc))

    thread = threading.Thread(target=_renew_periodically, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5.0)


def run_once(
    *,
    client: WorkerApiClient,
    input_resolver: WorkerInputResolver,
    clock: Clock = _default_clock,
    processors: Sequence[tuple[str, str]] = SUPPORTED_PROCESSORS,
    detector: ObjectDetector | None = None,
    tracker: ObjectTracker | None = None,
    ocr_adapter: ImageOcrAdapter | None = None,
    renew_interval_seconds: float | None = None,
    processing_profile: ProcessingProfile | None = None,
) -> RunOnceOutcome:
    """Claim at most one job, process it, submit its result, and return what happened.

    Tries each supported `(processor_name, processor_version)` in turn
    until one yields a job, or returns `claimed=False` once all are
    exhausted -- a normal, successful "no work" outcome, never an error.
    Never submits `queued`/`running` as a result: `process_job` only ever
    returns a terminal `WorkerResultV1`, and the input-resolution-gap and
    integrity-mismatch paths below both submit a terminal result too,
    never fabricating `SUCCEEDED`.

    `detector`/`tracker`/`ocr` are threaded straight through to
    `process_job` unchanged -- `None` (the default) means metadata-only
    processing, exactly as before this phase. `renew_interval_seconds`
    (`None` by default -- unchanged behavior), when set, wraps the
    `process_job` call in a background lease-heartbeat (`_lease_heartbeat`)
    for real analysis that may outlast the lease window it was claimed
    under (a large video's sampling + detection + OCR).
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

        evidence_record = _shim_evidence_record(job, resolved)

        batches_submitted = 0
        total_observations = 0
        video_frames_seen = 0
        last_video_frame: ExtractedFrame | None = None
        last_video_metadata: VideoMetadata | None = None

        def _submit_ocr_batch(
            frame: ExtractedFrame | None, image: Frame, meta: ImageMetadata | VideoMetadata
        ) -> None:
            nonlocal batches_submitted, total_observations
            nonlocal video_frames_seen, last_video_frame, last_video_metadata
            if ocr_adapter is None:
                return

            if frame is not None:
                ocr_meta = ImageMetadata(
                    width=meta.width,
                    height=meta.height,
                    format="mp4",
                    color_mode="RGB",
                    orientation=1,
                )
            else:
                ocr_meta = typing.cast(ImageMetadata, meta)

            if frame is not None:
                video_frames_seen += 1
                last_video_frame = frame
                last_video_metadata = typing.cast("VideoMetadata", meta)

            results = ocr_adapter.run(image, ocr_meta)
            if not results:
                return

            if frame is None:
                batch_config = OcrBatchConfig(batch_size=50, units_total=len(results))
                idempotency_prefix = f"{job.job_id}-ocr"
                for batch_submission in iter_image_ocr_batches(
                    results,
                    job=job,
                    idempotency_key_prefix=idempotency_prefix,
                    batch_config=batch_config,
                    completed_at=datetime.now(UTC),
                    mark_final_batch=False,
                ):
                    client.submit_batch(
                        job_id=job.job_id, claim_token=claim_token, submission=batch_submission
                    )
                    batches_submitted += 1
                    total_observations += len(batch_submission.observations)
            else:
                video_meta = typing.cast("VideoMetadata", meta)
                batch_submission = build_video_frame_ocr_batch(
                    results,
                    job=job,
                    frame=frame,
                    metadata=video_meta,
                    batch_id=f"{job.job_id}-frame-{frame.frame_number or frame.time_start_ms}",
                    batch_sequence=batches_submitted,
                    idempotency_key=(
                        f"{job.job_id}-frame-{frame.frame_number or frame.time_start_ms}"
                    ),
                    units_completed=video_frames_seen,
                    units_total=None,
                    observations_emitted_before=total_observations,
                    completed_at=datetime.now(UTC),
                )
                client.submit_batch(
                    job_id=job.job_id, claim_token=claim_token, submission=batch_submission
                )
                batches_submitted += 1
                total_observations += len(batch_submission.observations)

        heartbeat_ctx = (
            _lease_heartbeat(client, job.job_id, claim_token, renew_interval_seconds)
            if renew_interval_seconds is not None
            else nullcontext()
        )
        try:
            with heartbeat_ctx:
                profile = processing_profile or RAPID_PROFILE
                result = process_job(
                    job,
                    evidence=evidence_record,
                    resolver=StaticBytesResolver(payload=resolved.data),
                    detector=detector,
                    tracker=tracker,
                    ocr_adapter=ocr_adapter if profile.ocr_enabled else None,
                    sampling=SamplingRequest(
                        strategy=SamplingStrategy.UNIFORM_INTERVAL,
                        interval_ms=profile.baseline_interval_ms,
                        max_frames=profile.max_frames_per_chunk,
                    ),
                    submit_ocr_batch=_submit_ocr_batch,
                    stopwatch=None,
                )

                if result.status is WorkerStatus.SUCCEEDED:
                    media_batch = build_media_observation_batch(
                        result.observations,
                        job=job,
                        batch_sequence=batches_submitted,
                        idempotency_key_prefix=str(job.job_id),
                        observations_emitted_before=total_observations,
                        completed_at=clock(),
                        is_final=last_video_frame is None,
                    )
                    client.submit_batch(
                        job_id=job.job_id, claim_token=claim_token, submission=media_batch
                    )
                    batches_submitted += 1
                    total_observations += len(media_batch.observations)

                    # Frames are discovered incrementally.  A final, empty
                    # video-frame progress batch records completion only once
                    # frame extraction has finished, without buffering frames
                    # or pretending an earlier frame was known to be last.
                    if last_video_frame is not None and last_video_metadata is not None:
                        final_video_batch = build_video_frame_ocr_batch(
                            [],
                            job=job,
                            frame=last_video_frame,
                            metadata=last_video_metadata,
                            batch_id=f"{job.job_id}-frame-ocr-final",
                            batch_sequence=batches_submitted,
                            idempotency_key=f"{job.job_id}-frame-ocr-final",
                            units_completed=video_frames_seen,
                            units_total=video_frames_seen,
                            observations_emitted_before=total_observations,
                            completed_at=clock(),
                            is_final=True,
                        )
                        client.submit_batch(
                            job_id=job.job_id,
                            claim_token=claim_token,
                            submission=final_video_batch,
                        )
                        batches_submitted += 1

                    result = build_terminal_result(
                        job_id=job.job_id,
                        case_id=job.case_id,
                        evidence_id=job.evidence_id,
                        completed_at=clock(),
                    )
        except WorkerApiError:
            # A failed batch acknowledgement must not escape uncaught from a
            # claimed worker run.  The server may safely replay a retry of the
            # deterministic batch, while this attempt records one safe,
            # retryable terminal failure when `/result` remains reachable.
            result = build_terminal_result_failed(
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                error_code="media_batch_submission_failed",
                error_message="media observation batch submission failed",
                retryable=True,
                completed_at=clock(),
            )
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


@dataclass(frozen=True)
class AnalysisComponents:
    """The real local analysis components this worker process could load, if any."""

    detector: ObjectDetector | None
    tracker: ObjectTracker | None
    ocr_adapter: ImageOcrAdapter | None


def _build_analysis_components(settings: Settings) -> AnalysisComponents:
    """Build the real local detector/tracker/OCR components from configuration.

    Never crashes the CLI on its own: a missing/invalid model asset or an
    unusable OCR runtime is logged as a clear, structured warning, and the
    corresponding component is simply `None` -- this worker then falls
    back to metadata-only claiming (`_effective_processors`) for this run,
    an honest degraded mode, never a silent fabrication and never a hard
    crash that would also stop metadata-only jobs (which need no analysis
    component at all) from being processed. `main`'s `--require-analysis`
    flag turns a missing detector into a hard startup failure instead, for
    a deployment that wants to guarantee real detection is available
    before it will run at all. The tracker has no asset/runtime of its own
    to fail on (`IoUTracker` is pure Python) -- it is only ever built
    alongside a successfully-loaded detector, since tracking without
    detections to track is meaningless.
    """
    detector: ObjectDetector | None = None
    tracker: ObjectTracker | None = None
    try:
        detector = OnnxObjectDetector(
            config=DetectorConfig(
                model_path=settings.media_detector_model_path,
                expected_sha256=settings.media_detector_model_sha256,
                device=settings.media_detector_device,
                confidence_threshold=settings.media_detector_confidence_threshold,
                nms_threshold=settings.media_detector_nms_threshold,
            )
        )
        tracker = IoUTracker()
        logger.info("worker.analysis.detector_loaded", device=detector.device)
    except ModelAssetError as exc:
        logger.warning("worker.analysis.detector_unavailable", reason=str(exc))
    try:
        ocr_adapter = ImageOcrAdapter(
            config=OcrAdapterConfig(
                language=settings.media_ocr_language,
                min_confidence=settings.media_ocr_min_confidence,
            )
        )
        logger.info("worker.analysis.ocr_adapter_loaded", language=settings.media_ocr_language)
    except Exception as exc:
        logger.warning("worker.analysis.ocr_adapter_unavailable", reason=str(exc))
        ocr_adapter = None
    return AnalysisComponents(detector=detector, tracker=tracker, ocr_adapter=ocr_adapter)


@dataclass(frozen=True)
class RunLoopSummary:
    """What one `run_loop` call did before stopping -- for the CLI's exit code and for tests."""

    iterations: int
    jobs_processed: int
    consecutive_failures: int
    #: `"shutdown_requested"` (a clean stop, exit `0`) or
    #: `"max_consecutive_failures"` (a worsening problem, exit `1`).
    stopped_reason: str


def _install_signal_handlers(shutdown_event: threading.Event) -> None:
    """SIGINT/SIGTERM both request the same graceful shutdown: stop claiming new
    jobs, let whatever `run_once` iteration is already in flight finish and
    submit its terminal result normally, then return. Never force-kills or
    interrupts an in-progress `process_job` call."""

    def _handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("worker.loop.shutdown_signal_received", signal=signal.Signals(signum).name)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def run_loop(
    *,
    client: WorkerApiClient,
    input_resolver: WorkerInputResolver,
    shutdown_event: threading.Event,
    clock: Clock = _default_clock,
    processors: Sequence[tuple[str, str]] = SUPPORTED_PROCESSORS,
    detector: ObjectDetector | None = None,
    tracker: ObjectTracker | None = None,
    ocr_adapter: ImageOcrAdapter | None = None,
    poll_interval_seconds: float = 5.0,
    max_backoff_seconds: float = 60.0,
    max_consecutive_failures: int = 5,
    renew_interval_seconds: float | None = None,
    processing_profile: ProcessingProfile | None = None,
) -> RunLoopSummary:
    """Continuously claim-process-submit (via `run_once`) until `shutdown_event` is
    set or too many consecutive failures occur.

    `shutdown_event` is checked *before* every `run_once` call, never
    mid-call -- once it is set, this loop claims no further jobs; whatever
    iteration is already running (there is at most one, this loop is not
    concurrent) always finishes to a submitted terminal/deferred result or
    a clean "no work" return first, since that is `run_once`'s own
    unconditional contract -- there is no partial claim this loop could
    ever abandon. A successful "no work available" outcome resets the
    failure counter and sleeps `poll_interval_seconds` (via
    `shutdown_event.wait`, so a shutdown request wakes it immediately
    rather than after the full interval); a job actually processed also
    resets the counter but loops again immediately (more work may be
    queued). An exception from `run_once` (API unreachable, auth rejected)
    increments a consecutive-failure counter and sleeps a bounded
    exponential backoff (`poll_interval_seconds * 2**consecutive_failures`,
    capped at `max_backoff_seconds`); reaching `max_consecutive_failures`
    stops the loop entirely -- a real, worsening problem, not something to
    retry forever silently (see `main`'s exit-code mapping).
    """
    iterations = 0
    jobs_processed = 0
    consecutive_failures = 0
    while not shutdown_event.is_set():
        iterations += 1
        try:
            outcome = run_once(
                client=client,
                input_resolver=input_resolver,
                clock=clock,
                processors=processors,
                detector=detector,
                tracker=tracker,
                ocr_adapter=ocr_adapter,
                renew_interval_seconds=renew_interval_seconds,
                processing_profile=processing_profile,
            )
        except (WorkerAuthenticationError, WorkerApiError, InputResolutionUnavailableError) as exc:
            consecutive_failures += 1
            logger.error(
                "worker.loop.iteration_failed",
                reason=str(exc),
                consecutive_failures=consecutive_failures,
            )
            if consecutive_failures >= max_consecutive_failures:
                return RunLoopSummary(
                    iterations, jobs_processed, consecutive_failures, "max_consecutive_failures"
                )
            backoff = min(poll_interval_seconds * (2**consecutive_failures), max_backoff_seconds)
            shutdown_event.wait(backoff)
            continue

        consecutive_failures = 0
        if outcome.claimed:
            jobs_processed += 1
            logger.info(
                "worker.loop.job_processed",
                job_id=str(outcome.job_id),
                status=outcome.result_status,
            )
            continue
        shutdown_event.wait(poll_interval_seconds)

    return RunLoopSummary(iterations, jobs_processed, consecutive_failures, "shutdown_requested")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point:

    uv run python -m app.modules.media_processing.worker --once
    uv run python -m app.modules.media_processing.worker --loop
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.media_processing.worker",
        description=(
            "Claim and process compatible media-processing jobs against the internal "
            "worker API. --once processes at most one job then exits; --loop runs "
            "continuously until SIGINT/SIGTERM."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once", action="store_true", help="Run exactly one claim-process-submit cycle, then exit."
    )
    mode.add_argument(
        "--loop", action="store_true", help="Run continuously until SIGINT/SIGTERM, then exit."
    )
    parser.add_argument(
        "--require-analysis",
        action="store_true",
        help=(
            "Fail at startup (exit 1) instead of silently degrading to metadata-only "
            "claiming if the configured detector model asset cannot be loaded."
        ),
    )
    args = parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()
    try:
        client = _build_client(settings)
    except WorkerAuthenticationError as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1

    components = _build_analysis_components(settings)
    processing_profile = (
        RAPID_PROFILE if settings.media_processing_profile == "rapid" else DEEP_PROFILE
    )
    if args.require_analysis and components.detector is None:
        logger.error(
            "worker.cli.failed",
            reason="--require-analysis was set but no detector model asset could be loaded",
        )
        client.close()
        return 1
    processors = _effective_processors(components.detector)

    if args.once:
        try:
            outcome = run_once(
                client=client,
                input_resolver=LiveInputResolver(client),
                processors=processors,
                detector=components.detector,
                tracker=components.tracker,
                ocr_adapter=components.ocr_adapter,
                renew_interval_seconds=settings.media_worker_renew_interval_seconds,
                processing_profile=processing_profile,
            )
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

    shutdown_event = threading.Event()
    _install_signal_handlers(shutdown_event)
    try:
        summary = run_loop(
            client=client,
            input_resolver=LiveInputResolver(client),
            shutdown_event=shutdown_event,
            processors=processors,
            detector=components.detector,
            tracker=components.tracker,
            ocr_adapter=components.ocr_adapter,
            poll_interval_seconds=settings.media_worker_poll_interval_seconds,
            max_backoff_seconds=settings.media_worker_max_backoff_seconds,
            max_consecutive_failures=settings.media_worker_max_consecutive_failures,
            renew_interval_seconds=settings.media_worker_renew_interval_seconds,
            processing_profile=processing_profile,
        )
    finally:
        client.close()

    logger.info(
        "worker.cli.loop_done",
        iterations=summary.iterations,
        jobs_processed=summary.jobs_processed,
        stopped_reason=summary.stopped_reason,
    )
    return 0 if summary.stopped_reason == "shutdown_requested" else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE",
    "PROCESSOR_NAME_DETECTION",
    "PROCESSOR_NAME_METADATA",
    "PROCESSOR_VERSION",
    "SUPPORTED_PROCESSORS",
    "AnalysisComponents",
    "RunLoopSummary",
    "RunOnceOutcome",
    "main",
    "process_job",
    "run_loop",
    "run_once",
]
