"""Phase 7 Part 3 visual-benchmark adapters: detection, tracking, visual-text (OCR).

Narrow, replaceable engine protocols -- never the production
`analysis.interfaces.ObjectDetector`/`ObjectTracker`/`TextRecognizer`
protocols directly, since a benchmark run also needs to report which
*candidate* (model name/version/artifact hash/backend) actually produced a
result, a channel the production protocols have no need for. Every engine
still returns the same `ObjectDetection`/`TrackSegment`/`RecognizedText`
dataclasses production code already uses, and an accepted result can be
represented through the exact same canonical `ObservationV1`/provenance
seam (`build_observation_draft_for_*` below) -- proving compatibility
without this module ever writing an observation anywhere itself.

No real YOLO/ByteTrack/PaddleOCR model is downloaded, imported, or run
here. Unit tests exercise this module exclusively through the `Fake*`
engines below and never require a GPU, a model download, or a real
VIRAT/UFPR-ALPR/safe-unsafe-behaviour dataset.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ObservationV1
from app.modules.evaluation.models import BenchmarkRunStatus, BenchmarkRunV1, CandidateTask, SplitId
from app.modules.media_processing.analysis.interfaces import (
    ObjectDetection,
    RecognizedText,
    TrackSegment,
)
from app.modules.media_processing.image.geometry import PixelBoundingBox, to_normalized
from app.modules.media_processing.models import Frame, MediaObservationDraft
from app.modules.media_processing.provenance import (
    OBSERVATION_ANONYMOUS_TRACK_SEGMENT,
    OBSERVATION_OBJECT_DETECTION,
    OBSERVATION_OCR_TEXT_MENTION,
    build_extractor,
    draft_to_observation,
)
from app.modules.media_processing.visual_benchmark_metrics import (
    GroundTruthBox,
    GroundTruthTrack,
    character_error_rate,
    detection_precision_recall_map,
    field_extraction_prf,
    id_switches,
    idf1,
    median,
    mota,
    peak_memory_mb,
    percentile,
    word_error_rate,
)

RUNTIME_ENVIRONMENT = "phase7-part3-visual-benchmark-cli-v1"

#: A visual-text/plate-OCR sample has exactly one field to score (the
#: recognized text itself, e.g. a plate string) -- this is the single key
#: both `VisualTextBenchmarkSample.expected_fields` and this module's own
#: extraction must agree on for `field_extraction_prf` to score correctly.
VISUAL_TEXT_FIELD_KEY = "recognized_text"


class BenchmarkArtifactUnavailableError(Exception):
    """A local dataset/model artifact could not be found or loaded.

    Caught by `visual_benchmark.py` and converted into a safe
    `BenchmarkRunStatus.UNAVAILABLE` result -- never propagates to the CLI
    as an unhandled exception.
    """


class EngineError(Exception):
    """A named, safe per-sample engine failure -- never a raw third-party exception."""

    CATEGORIES = frozenset({"unreadable", "unsupported_format", "execution_failure"})

    def __init__(self, category: str, message: str) -> None:
        if category not in self.CATEGORIES:
            raise ValueError(f"unknown EngineError category '{category}'")
        super().__init__(message)
        self.category = category
        self.message = message


# --- Engine protocols and result wrappers --------------------------------


@dataclass(frozen=True)
class DetectorEngineResult:
    detections: tuple[ObjectDetection, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class DetectorEngine(Protocol):
    def detect(self, frame: Frame) -> DetectorEngineResult: ...


@dataclass(frozen=True)
class TrackerEngineResult:
    tracks: tuple[TrackSegment, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class TrackerEngine(Protocol):
    def track(
        self, detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
    ) -> TrackerEngineResult: ...


@dataclass(frozen=True)
class VisualTextEngineResult:
    texts: tuple[RecognizedText, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class VisualTextEngine(Protocol):
    def recognize_regions(self, image: Frame) -> VisualTextEngineResult: ...


# --- Fake engines (tests only) --------------------------------------------


@dataclass(frozen=True)
class FakeDetectorEngine:
    """Deterministic, test-only: returns a fixed detection list for every call.

    A frame shaped `UNREADABLE_SHAPE`/`UNSUPPORTED_FORMAT_SHAPE`/
    `EXECUTION_FAILURE_SHAPE` raises the matching `EngineError` instead --
    a shape-based marker (never pixel content) since a `Frame` has no
    natural "marker bytes" concept the way raw file bytes do.
    """

    UNREADABLE_SHAPE = (0, 0, 3)
    UNSUPPORTED_FORMAT_SHAPE = (1, 1, 1)
    EXECUTION_FAILURE_SHAPE = (2, 2, 1)

    fixed_detections: tuple[ObjectDetection, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-detector"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def detect(self, frame: Frame) -> DetectorEngineResult:
        shape = tuple(frame.shape)
        if shape == self.UNREADABLE_SHAPE:
            raise EngineError("unreadable", "sample could not be decoded")
        if shape == self.UNSUPPORTED_FORMAT_SHAPE:
            raise EngineError("unsupported_format", "sample format is not supported")
        if shape == self.EXECUTION_FAILURE_SHAPE:
            raise EngineError("execution_failure", "detector execution failed")
        return DetectorEngineResult(
            detections=self.fixed_detections,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


#: Marker labels a `FakeTrackerEngine` recognizes on an input detection to
#: simulate a per-sample failure -- detections carrying one of these labels
#: are never treated as real object classes.
TRACKER_FAKE_UNREADABLE_LABEL = "__BENCHMARK_FAKE_UNREADABLE__"
TRACKER_FAKE_UNSUPPORTED_LABEL = "__BENCHMARK_FAKE_UNSUPPORTED__"
TRACKER_FAKE_EXEC_FAIL_LABEL = "__BENCHMARK_FAKE_EXEC_FAIL__"


@dataclass(frozen=True)
class FakeTrackerEngine:
    """Deterministic, test-only: returns a fixed track list for every call."""

    fixed_tracks: tuple[TrackSegment, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-tracker"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def track(
        self, detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
    ) -> TrackerEngineResult:
        labels = {d.label for detections in detections_by_time_ms.values() for d in detections}
        if TRACKER_FAKE_UNREADABLE_LABEL in labels:
            raise EngineError("unreadable", "sample could not be decoded")
        if TRACKER_FAKE_UNSUPPORTED_LABEL in labels:
            raise EngineError("unsupported_format", "sample format is not supported")
        if TRACKER_FAKE_EXEC_FAIL_LABEL in labels:
            raise EngineError("execution_failure", "tracker execution failed")
        return TrackerEngineResult(
            tracks=self.fixed_tracks,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


@dataclass(frozen=True)
class FakeVisualTextEngine:
    """Deterministic, test-only: returns fixed recognized text for every call."""

    UNREADABLE_SHAPE = (0, 0, 3)
    UNSUPPORTED_FORMAT_SHAPE = (1, 1, 1)
    EXECUTION_FAILURE_SHAPE = (2, 2, 1)

    fixed_texts: tuple[RecognizedText, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-visual-text"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def recognize_regions(self, image: Frame) -> VisualTextEngineResult:
        shape = tuple(image.shape)
        if shape == self.UNREADABLE_SHAPE:
            raise EngineError("unreadable", "sample could not be decoded")
        if shape == self.UNSUPPORTED_FORMAT_SHAPE:
            raise EngineError("unsupported_format", "sample format is not supported")
        if shape == self.EXECUTION_FAILURE_SHAPE:
            raise EngineError("execution_failure", "OCR execution failed")
        return VisualTextEngineResult(
            texts=self.fixed_texts,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


# --- Benchmark sample types -------------------------------------------------


@dataclass(frozen=True)
class DetectionBenchmarkSample:
    sample_id: str
    frame: Frame
    ground_truth: tuple[GroundTruthBox, ...] | None = None


@dataclass(frozen=True)
class TrackingBenchmarkSample:
    """Ground-truth *detections* per timestamp, used as the tracker's input.

    Benchmarking the tracker against ground-truth detections (rather than
    a real detector's own output) isolates tracking quality from detection
    quality -- a standard, legitimate tracking-benchmark methodology, and
    the only one this harness can exercise without also running a real
    detector over every frame of a real video first.
    """

    sample_id: str
    detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
    ground_truth: tuple[GroundTruthTrack, ...] | None = None


@dataclass(frozen=True)
class VisualTextBenchmarkSample:
    sample_id: str
    image: Frame
    reference_text: str | None = None
    expected_fields: Mapping[str, str] | None = None


# --- Aggregation and BenchmarkRunV1 construction ---------------------------


@dataclass
class _RunAccumulator:
    latencies_ms: list[float] = field(default_factory=list)
    success_count: int = 0
    failure_by_category: dict[str, int] = field(default_factory=dict)


def _record_failure(accumulator: _RunAccumulator, category: str) -> None:
    accumulator.failure_by_category[category] = accumulator.failure_by_category.get(category, 0) + 1


def _base_metrics(accumulator: _RunAccumulator, sample_count: int) -> dict[str, float | int | None]:
    metrics: dict[str, float | int | None] = {
        "latency_ms": median(accumulator.latencies_ms),
        "ram_mb": peak_memory_mb(),
        "sample_count": sample_count,
        "sample_success_count": accumulator.success_count,
        "sample_failure_count": sample_count - accumulator.success_count,
        "latency_p50_ms": percentile(accumulator.latencies_ms, 50.0),
        "latency_p95_ms": percentile(accumulator.latencies_ms, 95.0),
        "latency_p99_ms": percentile(accumulator.latencies_ms, 99.0),
    }
    for category, count in accumulator.failure_by_category.items():
        metrics[f"sample_failure_count_{category}"] = count
    return metrics


def _new_run_id(dataset_id: str, candidate_id: str) -> str:
    return f"{dataset_id}-{candidate_id}-{uuid4().hex[:12]}"


def run_detection_benchmark(
    *,
    engine: DetectorEngine,
    samples: Sequence[DetectionBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config: dict[str, object],
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_detection_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    all_predictions: list[ObjectDetection] = []
    all_ground_truth: list[GroundTruthBox] = []
    any_ground_truth_present = False
    engine_result: DetectorEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.detect(sample.frame)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        all_predictions.extend(engine_result.detections)
        if sample.ground_truth is not None:
            any_ground_truth_present = True
            all_ground_truth.extend(sample.ground_truth)

    precision, recall, mean_ap = detection_precision_recall_map(
        all_predictions, all_ground_truth if any_ground_truth_present else None
    )
    metrics: dict[str, float | int | None] = {
        "precision": precision,
        "recall": recall,
        "map": mean_ap,
        "vram_mb": None,
        **_base_metrics(accumulator, len(samples)),
    }

    if accumulator.success_count == 0:
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=_new_run_id(dataset_id, candidate_id),
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task=CandidateTask.DETECTION,
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="unavailable",
            inference_config_hash=inference_config_hash,
            artifact_sha256=None,
            metrics=metrics,
            started_at=now,
            completed_at=now,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe=f"all {len(samples)} sample(s) failed detection",
        )

    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=_new_run_id(dataset_id, candidate_id),
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task=CandidateTask.DETECTION,
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile=engine_result.backend,
        inference_config_hash=inference_config_hash,
        artifact_sha256=engine_result.model_sha256,
        metrics=metrics,
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.SUCCEEDED,
        failure_reason_safe=None,
    )


def run_tracking_benchmark(
    *,
    engine: TrackerEngine,
    samples: Sequence[TrackingBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config: dict[str, object],
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_tracking_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    idf1_values: list[float] = []
    mota_values: list[float] = []
    id_switch_total = 0
    any_ground_truth_present = False
    engine_result: TrackerEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.track(sample.detections_by_time_ms)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        if sample.ground_truth is not None:
            any_ground_truth_present = True
            sample_idf1 = idf1(list(engine_result.tracks), sample.ground_truth)
            sample_mota = mota(list(engine_result.tracks), sample.ground_truth)
            sample_switches = id_switches(list(engine_result.tracks), sample.ground_truth)
            if sample_idf1 is not None:
                idf1_values.append(sample_idf1)
            if sample_mota is not None:
                mota_values.append(sample_mota)
            if sample_switches is not None:
                id_switch_total += sample_switches

    metrics: dict[str, float | int | None] = {
        "idf1": median(idf1_values) if any_ground_truth_present else None,
        "mota": median(mota_values) if any_ground_truth_present else None,
        "id_switches": id_switch_total if any_ground_truth_present else None,
        # Always None in this harness -- see visual_benchmark_metrics.hota's docstring.
        "hota": None,
        "vram_mb": None,
        **_base_metrics(accumulator, len(samples)),
    }

    if accumulator.success_count == 0:
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=_new_run_id(dataset_id, candidate_id),
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task=CandidateTask.TRACKING,
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="unavailable",
            inference_config_hash=inference_config_hash,
            artifact_sha256=None,
            metrics=metrics,
            started_at=now,
            completed_at=now,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe=f"all {len(samples)} sample(s) failed tracking",
        )

    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=_new_run_id(dataset_id, candidate_id),
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task=CandidateTask.TRACKING,
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile=engine_result.backend,
        inference_config_hash=inference_config_hash,
        artifact_sha256=engine_result.model_sha256,
        metrics=metrics,
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.SUCCEEDED,
        failure_reason_safe=None,
    )


def run_visual_text_benchmark(
    *,
    engine: VisualTextEngine,
    samples: Sequence[VisualTextBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config: dict[str, object],
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_visual_text_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    cer_values: list[float] = []
    wer_values: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    engine_result: VisualTextEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.recognize_regions(sample.image)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        recognized_text = " ".join(t.text for t in engine_result.texts)
        if sample.reference_text is not None:
            cer = character_error_rate(sample.reference_text, recognized_text)
            wer = word_error_rate(sample.reference_text, recognized_text)
            if cer is not None:
                cer_values.append(cer)
            if wer is not None:
                wer_values.append(wer)
        if sample.expected_fields is not None:
            extracted_fields = {VISUAL_TEXT_FIELD_KEY: recognized_text} if recognized_text else {}
            precision, recall, f1 = field_extraction_prf(sample.expected_fields, extracted_fields)
            if precision is not None:
                precisions.append(precision)
            if recall is not None:
                recalls.append(recall)
            if f1 is not None:
                f1s.append(f1)

    metrics: dict[str, float | int | None] = {
        "character_error_rate": median(cer_values),
        "word_error_rate": median(wer_values),
        "field_extraction_precision": median(precisions),
        "field_extraction_recall": median(recalls),
        "field_extraction_f1": median(f1s),
        **_base_metrics(accumulator, len(samples)),
    }
    metrics.pop("sample_count", None)
    metrics["sample_count"] = len(samples)

    if accumulator.success_count == 0:
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=_new_run_id(dataset_id, candidate_id),
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task=CandidateTask.VISUAL_TEXT,
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="unavailable",
            inference_config_hash=inference_config_hash,
            artifact_sha256=None,
            metrics=metrics,
            started_at=now,
            completed_at=now,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe=f"all {len(samples)} sample(s) failed visual-text recognition",
        )

    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=_new_run_id(dataset_id, candidate_id),
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task=CandidateTask.VISUAL_TEXT,
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile=engine_result.backend,
        inference_config_hash=inference_config_hash,
        artifact_sha256=engine_result.model_sha256,
        metrics=metrics,
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.SUCCEEDED,
        failure_reason_safe=None,
    )


# --- Canonical observation/provenance compatibility proof -----------------
#
# None of these are called by `run_*_benchmark` above -- a benchmark result
# is evaluation metadata, never primary evidence, and this module creates
# no new raw-evidence persistence path. They exist solely to prove (and be
# tested proving) that an accepted detection/track/OCR result *could* flow
# through the exact same canonical `ObservationV1`/provenance seam
# production detection already uses, with no alternate observation format.


def build_observation_draft_for_detection(
    detection: ObjectDetection, *, image_width: int, image_height: int, locator: SourceLocator
) -> MediaObservationDraft:
    normalized = to_normalized(detection.box, image_width=image_width, image_height=image_height)
    return MediaObservationDraft(
        observation_type=OBSERVATION_OBJECT_DETECTION,
        locator=locator.model_copy(update={"bbox_xyxy_normalized": normalized}),
        confidence=detection.confidence,
        attributes={"label": detection.label, **dict(detection.attributes)},
    )


def build_observation_draft_for_track_segment(
    track: TrackSegment, *, locator: SourceLocator
) -> MediaObservationDraft:
    return MediaObservationDraft(
        observation_type=OBSERVATION_ANONYMOUS_TRACK_SEGMENT,
        locator=locator,
        confidence=track.quality,
        attributes={"label": track.label, **dict(track.attributes)},
        discriminator=track.local_track_id,
    )


def build_observation_draft_for_visual_text(
    text: RecognizedText, *, image_width: int, image_height: int, locator: SourceLocator
) -> MediaObservationDraft:
    normalized = to_normalized(text.box, image_width=image_width, image_height=image_height)
    return MediaObservationDraft(
        observation_type=OBSERVATION_OCR_TEXT_MENTION,
        locator=locator.model_copy(update={"bbox_xyxy_normalized": normalized}),
        confidence=text.confidence,
        entity_text=text.text,
        entity_type_hint="visual_text_region",
        attributes=dict(text.attributes),
    )


def observation_for_draft(
    draft: MediaObservationDraft,
    *,
    case_id: UUID,
    evidence_id: UUID,
    processor_name: str,
    processor_version: str,
    model_version: str,
    created_at: datetime,
) -> ObservationV1:
    """Build a real `ObservationV1` from a benchmark-produced draft.

    A thin, test-facing wrapper around the exact `provenance.build_extractor`/
    `draft_to_observation` functions production detection already calls --
    proving compatibility by using the identical function, not a
    lookalike.
    """
    extractor: Extractor = build_extractor(
        processor_name=processor_name,
        processor_version=processor_version,
        model_version=model_version,
    )
    return draft_to_observation(
        case_id=case_id,
        evidence_id=evidence_id,
        draft=draft,
        extractor=extractor,
        created_at=created_at,
    )


# --- Local benchmark manifests (invented format -- see module docstring) --


def _resolve_within(dataset_dir: Path, relative_path: str) -> Path:
    resolved = (dataset_dir / relative_path).resolve()
    if not str(resolved).startswith(str(dataset_dir.resolve())):
        raise BenchmarkArtifactUnavailableError(
            "benchmark manifest referenced a path outside the dataset directory"
        )
    return resolved


def _decode_image(path: Path) -> Frame:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow/numpy are core deps
        raise BenchmarkArtifactUnavailableError(
            "an image codec dependency is unavailable in this environment"
        ) from exc
    try:
        with Image.open(path) as image:
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
    except (OSError, ValueError) as exc:
        raise BenchmarkArtifactUnavailableError(
            f"could not decode a benchmark manifest image for sample referencing '{path.name}'"
        ) from exc


def load_detection_benchmark_manifest(dataset_dir: Path) -> list[DetectionBenchmarkSample]:
    """Reads `dataset_dir/benchmark_manifest.jsonl` -- see module docstring for the format.

    This is *this task's own* invented local manifest shape, not the real
    VIRAT/UFPR-ALPR/safe-unsafe-behaviour annotation format (unverified in
    this environment) -- converting real annotations into this shape is
    deferred to Aditya's MacBook pre-flight.
    """
    manifest_path = dataset_dir / "benchmark_manifest.jsonl"
    if not manifest_path.is_file():
        raise BenchmarkArtifactUnavailableError(
            "no benchmark_manifest.jsonl found in the configured dataset directory"
        )
    samples: list[DetectionBenchmarkSample] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        image_path = _resolve_within(dataset_dir, record["image_path"])
        ground_truth_raw = record.get("ground_truth_boxes")
        ground_truth = (
            tuple(
                GroundTruthBox(
                    label=box["label"],
                    box=PixelBoundingBox(
                        x_min=box["box_xyxy"][0],
                        y_min=box["box_xyxy"][1],
                        x_max=box["box_xyxy"][2],
                        y_max=box["box_xyxy"][3],
                    ),
                )
                for box in ground_truth_raw
            )
            if ground_truth_raw is not None
            else None
        )
        samples.append(
            DetectionBenchmarkSample(
                sample_id=record["sample_id"],
                frame=_decode_image(image_path),
                ground_truth=ground_truth,
            )
        )
    return samples


def _detections_from_records(records: list[dict[str, object]]) -> list[ObjectDetection]:
    return [
        ObjectDetection(
            label=str(record["label"]),
            confidence=float(record["confidence"]),  # type: ignore[arg-type]
            box=PixelBoundingBox(
                x_min=record["box_xyxy"][0],  # type: ignore[index]
                y_min=record["box_xyxy"][1],  # type: ignore[index]
                x_max=record["box_xyxy"][2],  # type: ignore[index]
                y_max=record["box_xyxy"][3],  # type: ignore[index]
            ),
        )
        for record in records
    ]


def load_tracking_benchmark_manifest(dataset_dir: Path) -> list[TrackingBenchmarkSample]:
    """Reads `dataset_dir/tracking_manifest.jsonl`.

    An invented format (see the module docstring): one JSON object per
    line, per sequence, naming ground-truth *detections* per timestamp
    (used as the tracker's input -- see `TrackingBenchmarkSample`) plus
    optional ground-truth tracks to score against. No image is decoded for
    this manifest at all -- tracking is benchmarked purely over
    already-boxed detections.
    """
    manifest_path = dataset_dir / "tracking_manifest.jsonl"
    if not manifest_path.is_file():
        raise BenchmarkArtifactUnavailableError(
            "no tracking_manifest.jsonl found in the configured dataset directory"
        )
    samples: list[TrackingBenchmarkSample] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        detections_by_time_ms = {
            int(time_ms): _detections_from_records(records)
            for time_ms, records in record["detections_by_time_ms"].items()
        }
        ground_truth_raw = record.get("ground_truth_tracks")
        ground_truth = (
            tuple(
                GroundTruthTrack(
                    track_id=track["track_id"],
                    label=track["label"],
                    boxes_by_time_ms={
                        int(time_ms): PixelBoundingBox(
                            x_min=box[0], y_min=box[1], x_max=box[2], y_max=box[3]
                        )
                        for time_ms, box in track["boxes_by_time_ms"].items()
                    },
                )
                for track in ground_truth_raw
            )
            if ground_truth_raw is not None
            else None
        )
        samples.append(
            TrackingBenchmarkSample(
                sample_id=record["sample_id"],
                detections_by_time_ms=detections_by_time_ms,
                ground_truth=ground_truth,
            )
        )
    return samples


def load_visual_text_benchmark_manifest(dataset_dir: Path) -> list[VisualTextBenchmarkSample]:
    """Reads `dataset_dir/visual_text_manifest.jsonl`.

    An invented format, not a real dataset annotation shape -- see the
    module docstring.
    """
    manifest_path = dataset_dir / "visual_text_manifest.jsonl"
    if not manifest_path.is_file():
        raise BenchmarkArtifactUnavailableError(
            "no visual_text_manifest.jsonl found in the configured dataset directory"
        )
    samples: list[VisualTextBenchmarkSample] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        image_path = _resolve_within(dataset_dir, record["image_path"])
        samples.append(
            VisualTextBenchmarkSample(
                sample_id=record["sample_id"],
                image=_decode_image(image_path),
                reference_text=record.get("reference_text"),
                expected_fields=record.get("expected_fields"),
            )
        )
    return samples


__all__ = [
    "RUNTIME_ENVIRONMENT",
    "VISUAL_TEXT_FIELD_KEY",
    "BenchmarkArtifactUnavailableError",
    "DetectionBenchmarkSample",
    "DetectorEngine",
    "DetectorEngineResult",
    "EngineError",
    "FakeDetectorEngine",
    "FakeTrackerEngine",
    "FakeVisualTextEngine",
    "TRACKER_FAKE_EXEC_FAIL_LABEL",
    "TRACKER_FAKE_UNREADABLE_LABEL",
    "TRACKER_FAKE_UNSUPPORTED_LABEL",
    "TrackerEngine",
    "TrackerEngineResult",
    "TrackingBenchmarkSample",
    "VisualTextBenchmarkSample",
    "VisualTextEngine",
    "VisualTextEngineResult",
    "build_observation_draft_for_detection",
    "build_observation_draft_for_track_segment",
    "build_observation_draft_for_visual_text",
    "load_detection_benchmark_manifest",
    "load_tracking_benchmark_manifest",
    "load_visual_text_benchmark_manifest",
    "observation_for_draft",
    "run_detection_benchmark",
    "run_tracking_benchmark",
    "run_visual_text_benchmark",
]
