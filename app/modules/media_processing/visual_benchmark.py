"""Phase 7 Part 3 visual-benchmark orchestration: the one entry point the CLI calls.

`run_benchmark` validates the requested dataset/candidate against Part 1's
frozen catalogue and this task's own Part 3 scope, checks that both the
dataset's and the candidate's licence are cleared for a real run, resolves
local roots from explicit configuration only, checks artifact availability
*before* attempting to execute anything, dispatches to the right adapter
(`visual_benchmark_adapters.py`), and writes a safe aggregate JSON result.
It never downloads anything, never writes to Neo4j/PostgreSQL/MinIO, and
never fabricates a `succeeded` result for a request whose local artifacts
or licence clearance are missing.

Distinct from the pre-existing `media_processing.benchmark` (a real-time
processing-throughput benchmark for the production pipeline) -- this
module evaluates Phase 7 Part 1 *candidate models* against Part 1's
evaluation contracts, an entirely separate concern.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.evaluation.models import (
    BenchmarkRunStatus,
    BenchmarkRunV1,
    CandidateTask,
    DatasetManifestEntryV1,
    ModelCandidateV1,
    SplitId,
)
from app.modules.media_processing.analysis.interfaces import TrackSegment
from app.modules.media_processing.visual_benchmark_adapters import (
    BenchmarkArtifactUnavailableError,
    DetectorEngine,
    EngineError,
    TrackerEngine,
    VisualTextEngine,
    load_detection_benchmark_manifest,
    load_tracking_benchmark_manifest,
    load_visual_text_benchmark_manifest,
    run_detection_benchmark,
    run_tracking_benchmark,
    run_visual_text_benchmark,
)
from app.modules.media_processing.visual_benchmark_validation import (
    LicenseNotClearedError,
    benchmark_inference_config_hash,
    reject_private_local_paths,
    require_license_cleared_for_real_execution,
    resolve_benchmark_data_root,
    resolve_benchmark_output_root,
    resolve_model_cache_root,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)

#: Every `local_path_placeholder` in the frozen manifest starts with this
#: repo-relative prefix; stripping it lets `TRACEX_BENCHMARK_DATA_ROOT`
#: point anywhere on a real machine while the manifest's own placeholder
#: stays a portable, dataset-relative suffix -- same convention any other
#: Phase 7 benchmark part building on this manifest would use.
_LOCAL_DATA_PREFIX = "local-data/"


def dataset_subdirectory(dataset: DatasetManifestEntryV1, data_root: Path) -> Path:
    placeholder = dataset.local_path_placeholder
    suffix = (
        placeholder[len(_LOCAL_DATA_PREFIX) :]
        if placeholder.startswith(_LOCAL_DATA_PREFIX)
        else placeholder
    )
    return data_root / suffix


@dataclass(frozen=True)
class VerifiedModelArtifact:
    """Verified-during-MacBook-pre-flight model metadata.

    Every field must come from an artifact Aditya's pre-flight actually
    inspected -- this module never invents a model name, version, or hash.
    """

    model_name: str
    model_version: str
    model_sha256: str


def _build_real_detector_engine(
    *, model_cache_root: Path, artifact: VerifiedModelArtifact, variant: str
) -> DetectorEngine:
    """Best-effort real Ultralytics YOLO11 wiring.

    This session cannot install or exercise Ultralytics, so the exact API
    called here is written from its documented public shape and may need a
    small adjustment once Aditya's MacBook pre-flight confirms the
    actually-installed version's API -- any mismatch degrades to a safe
    `BenchmarkArtifactUnavailableError`, never a crash or a fabricated
    result. See `docs/runbooks/local-development.md`.
    """
    try:
        from ultralytics import YOLO  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "ultralytics is not installed in this environment -- run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md before a real "
            "YOLO11 benchmark run"
        ) from exc

    try:
        weights_path = model_cache_root / variant / "weights.pt"
        model = YOLO(str(weights_path))
    except Exception as exc:  # noqa: BLE001 - any construction failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not construct the local YOLO11 engine with the configured model cache "
            "-- verify the installed ultralytics API matches this adapter during "
            "MacBook pre-flight"
        ) from exc

    from app.modules.media_processing.analysis.interfaces import ObjectDetection
    from app.modules.media_processing.image.geometry import PixelBoundingBox
    from app.modules.media_processing.visual_benchmark_adapters import DetectorEngineResult

    class _RealDetectorEngine:
        def detect(self, frame: object) -> DetectorEngineResult:
            results = model(frame, verbose=False)
            detections = [
                ObjectDetection(
                    label=str(result.names[int(box.cls[0])]),
                    confidence=float(box.conf[0]),
                    box=PixelBoundingBox(
                        x_min=float(box.xyxy[0][0]),
                        y_min=float(box.xyxy[0][1]),
                        x_max=float(box.xyxy[0][2]),
                        y_max=float(box.xyxy[0][3]),
                    ),
                )
                for result in results
                for box in result.boxes
            ]
            device = str(getattr(model, "device", "cpu"))
            return DetectorEngineResult(
                detections=tuple(detections),
                backend=device,
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealDetectorEngine()


def _build_real_tracker_engine(*, artifact: VerifiedModelArtifact) -> TrackerEngine:
    """Best-effort real Ultralytics ByteTrack wiring.

    Uses Ultralytics' own `BYTETracker` class directly -- the identical
    tracker `model.track(..., tracker='bytetrack.yaml')` uses internally
    -- against externally-supplied per-timestamp detections, rather than
    running a full YOLO detector first. This matches
    `TrackingBenchmarkSample`'s own design (see its docstring): isolating
    tracking quality from detection quality is the whole point of a
    tracking-only benchmark. `bytetrack.yaml` ships bundled inside the
    `ultralytics` package itself -- no separate model weight or download
    is needed for ByteTrack specifically, since it is a Kalman-filter/
    Hungarian-matching association algorithm, not a learned model.

    This session cannot install or exercise `ultralytics`, so the exact
    internal API called here (`BYTETracker.update()`'s expected input
    shape) is written from its documented/observed public shape and may
    need a small adjustment once Aditya's MacBook pre-flight confirms the
    actually-installed version's API -- any mismatch degrades to a safe
    `BenchmarkArtifactUnavailableError`, never a crash or a fabricated
    result. See `docs/runbooks/local-development.md`.
    """
    try:
        from ultralytics.trackers.byte_tracker import BYTETracker  # type: ignore[import-not-found]
        from ultralytics.utils import (  # type: ignore[import-not-found]
            IterableSimpleNamespace,
            yaml_load,
        )
        from ultralytics.utils.checks import check_yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "ultralytics is not installed in this environment -- run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md before a real "
            "ByteTrack benchmark run"
        ) from exc

    try:
        tracker_config = IterableSimpleNamespace(**yaml_load(check_yaml("bytetrack.yaml")))
    except Exception as exc:  # noqa: BLE001 - any config-loading failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not load Ultralytics' bundled bytetrack.yaml tracker configuration "
            "-- verify the installed ultralytics API matches this adapter during "
            "MacBook pre-flight"
        ) from exc

    from app.modules.media_processing.analysis.interfaces import ObjectDetection
    from app.modules.media_processing.image.geometry import PixelBoundingBox
    from app.modules.media_processing.visual_benchmark_adapters import (
        EngineError,
        TrackerEngineResult,
    )

    def _detections_as_boxes_like(detections: list[ObjectDetection], labels: list[str]) -> object:
        """A minimal duck-typed stand-in for `ultralytics.engine.results.Boxes`.

        `BYTETracker.update()` reads `.xywh`/`.xyxy`/`.conf`/`.cls` as
        numpy arrays off its `results` argument -- constructed here
        directly from our own `ObjectDetection`s rather than requiring a
        real Ultralytics `Boxes` instance.
        """
        from types import SimpleNamespace

        import numpy as np

        xyxy = np.array(
            [[d.box.x_min, d.box.y_min, d.box.x_max, d.box.y_max] for d in detections],
            dtype=float,
        )
        xywh = np.column_stack(
            [
                (xyxy[:, 0] + xyxy[:, 2]) / 2.0,
                (xyxy[:, 1] + xyxy[:, 3]) / 2.0,
                xyxy[:, 2] - xyxy[:, 0],
                xyxy[:, 3] - xyxy[:, 1],
            ]
        )
        conf = np.array([d.confidence for d in detections], dtype=float)
        cls = np.array([labels.index(d.label) for d in detections], dtype=float)
        return SimpleNamespace(xyxy=xyxy, xywh=xywh, conf=conf, cls=cls)

    @dataclass
    class _AccumulatingTrack:
        label: str
        boxes_by_time_ms: dict[int, PixelBoundingBox] = field(default_factory=dict)

    class _RealByteTrackEngine:
        def track(
            self, detections_by_time_ms: Mapping[int, Sequence[ObjectDetection]]
        ) -> TrackerEngineResult:
            try:
                tracker = BYTETracker(tracker_config, frame_rate=30)
            except Exception as exc:  # noqa: BLE001 - construction failure is a safe "unavailable"
                raise EngineError(
                    "execution_failure", "could not construct the ByteTrack tracker instance"
                ) from exc

            labels = sorted(
                {d.label for detections in detections_by_time_ms.values() for d in detections}
            )
            accumulated: dict[int, _AccumulatingTrack] = {}
            for time_ms in sorted(detections_by_time_ms):
                detections = list(detections_by_time_ms[time_ms])
                if not detections:
                    continue
                boxes_like = _detections_as_boxes_like(detections, labels)
                try:
                    import numpy as np

                    tracked_rows = np.atleast_2d(tracker.update(boxes_like))
                except Exception as exc:  # noqa: BLE001 - a single bad frame is a safe failure
                    raise EngineError(
                        "execution_failure", "ByteTrack update failed for a frame"
                    ) from exc
                for row in tracked_rows:
                    if row.size < 5:
                        continue
                    x1, y1, x2, y2, track_id = row[:5]
                    class_index = int(row[6]) if row.size > 6 else 0
                    label = labels[class_index] if 0 <= class_index < len(labels) else labels[0]
                    entry = accumulated.setdefault(int(track_id), _AccumulatingTrack(label=label))
                    entry.boxes_by_time_ms[time_ms] = PixelBoundingBox(
                        x_min=float(x1), y_min=float(y1), x_max=float(x2), y_max=float(y2)
                    )

            tracks = tuple(
                TrackSegment(
                    local_track_id=str(track_id),
                    label=track.label,
                    start_time_ms=min(track.boxes_by_time_ms),
                    end_time_ms=max(track.boxes_by_time_ms),
                    boxes_by_time_ms=dict(track.boxes_by_time_ms),
                    quality=1.0 if len(track.boxes_by_time_ms) > 1 else 0.5,
                )
                for track_id, track in accumulated.items()
            )
            return TrackerEngineResult(
                tracks=tracks,
                # ByteTrack's association step (Kalman filter + Hungarian
                # matching) has no learned weights and no GPU path -- it
                # genuinely always runs on CPU regardless of what device
                # ran any upstream detector, so "cpu" here is an observed
                # fact about the algorithm, not an assumption.
                backend="cpu",
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealByteTrackEngine()


def _build_real_visual_text_engine(
    *, model_cache_root: Path, artifact: VerifiedModelArtifact
) -> VisualTextEngine:
    """Best-effort real PaddleOCR wiring, applied to visual-text/plate crops."""
    try:
        import paddleocr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "paddleocr is not installed in this environment -- run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md before a real "
            "visual-text benchmark run"
        ) from exc

    try:
        weights_dir = model_cache_root / "paddleocr-lightweight-visual-text"
        engine = paddleocr.PaddleOCR(det_model_dir=str(weights_dir), rec_model_dir=str(weights_dir))
    except Exception as exc:  # noqa: BLE001 - any construction failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not construct the local PaddleOCR engine with the configured "
            "model cache -- verify the installed paddleocr API matches this "
            "adapter during MacBook pre-flight"
        ) from exc

    from app.modules.media_processing.analysis.interfaces import RecognizedText
    from app.modules.media_processing.image.geometry import PixelBoundingBox
    from app.modules.media_processing.visual_benchmark_adapters import VisualTextEngineResult

    class _RealVisualTextEngine:
        def recognize_regions(self, image: object) -> VisualTextEngineResult:
            result = engine.ocr(image, cls=True)
            texts = [
                RecognizedText(
                    text=line[1][0],
                    confidence=float(line[1][1]),
                    box=PixelBoundingBox(
                        x_min=min(p[0] for p in line[0]),
                        y_min=min(p[1] for p in line[0]),
                        x_max=max(p[0] for p in line[0]),
                        y_max=max(p[1] for p in line[0]),
                    ),
                )
                for page in (result or [])
                for line in (page or [])
            ]
            return VisualTextEngineResult(
                texts=tuple(texts),
                backend="paddleocr-cpu",
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealVisualTextEngine()


def _unavailable_run(
    *,
    candidate: ModelCandidateV1,
    dataset: DatasetManifestEntryV1,
    split_id: SplitId,
    reason: str,
    now: datetime,
) -> BenchmarkRunV1:
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=f"{dataset.dataset_id}-{candidate.candidate_id}-{uuid4().hex[:12]}",
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        task=candidate.task,
        runtime_environment="phase7-part3-visual-benchmark-cli-v1",
        hardware_profile="unavailable",
        inference_config_hash="0" * 64,
        artifact_sha256=None,
        metrics={},
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.UNAVAILABLE,
        failure_reason_safe=reason,
    )


def run_benchmark(
    *,
    dataset_id: str,
    candidate_id: str,
    data_root: Path | None = None,
    model_cache_root: Path | None = None,
    output_root: Path | None = None,
    split_id: SplitId = SplitId.DEVELOPMENT,
    verified_model_artifact: VerifiedModelArtifact | None = None,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    """Validate, resolve artifacts, run one benchmark, and write a safe result.

    Never raises for a missing local artifact or an unresolved licence --
    both become a returned `BenchmarkRunStatus.UNAVAILABLE` result. Still
    raises for a genuinely invalid request (`UnknownDatasetError`/
    `UnknownCandidateError`/`UnsupportedCombinationError`/
    `BenchmarkConfigError`): those are caller bugs, not benchmark outcomes.
    """
    now = now or datetime.now(UTC)
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, dataset_id)
    candidate = validate_candidate_id(catalog, candidate_id)
    validate_dataset_candidate_pair(dataset, candidate)

    resolved_data_root = resolve_benchmark_data_root(data_root)
    resolved_output_root = resolve_benchmark_output_root(output_root)
    resolved_model_cache_root = resolve_model_cache_root(model_cache_root)
    dataset_dir = dataset_subdirectory(dataset, resolved_data_root)

    inference_config: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "dataset_id": dataset.dataset_id,
        "task": candidate.task.value,
    }
    if verified_model_artifact is not None:
        inference_config["model_name"] = verified_model_artifact.model_name
        inference_config["model_version"] = verified_model_artifact.model_version
    inference_config_hash = benchmark_inference_config_hash(inference_config)

    try:
        require_license_cleared_for_real_execution(dataset, candidate)
    except LicenseNotClearedError as exc:
        run = _unavailable_run(
            candidate=candidate, dataset=dataset, split_id=split_id, reason=str(exc), now=now
        )
        _write_result(run, resolved_output_root)
        return run

    if not dataset_dir.is_dir():
        run = _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="configured dataset directory does not exist locally",
            now=now,
        )
        _write_result(run, resolved_output_root)
        return run

    if verified_model_artifact is None:
        run = _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="verified model artifact metadata (name/version/sha256) is required "
            "for a real benchmark run",
            now=now,
        )
        _write_result(run, resolved_output_root)
        return run

    try:
        if candidate.task == CandidateTask.DETECTION:
            run = _run_detection(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                model_cache_root=resolved_model_cache_root,
                artifact=verified_model_artifact,
                split_id=split_id,
                inference_config=inference_config,
                inference_config_hash=inference_config_hash,
                now=now,
            )
        elif candidate.task == CandidateTask.TRACKING:
            run = _run_tracking(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                artifact=verified_model_artifact,
                split_id=split_id,
                inference_config=inference_config,
                inference_config_hash=inference_config_hash,
                now=now,
            )
        else:
            run = _run_visual_text(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                model_cache_root=resolved_model_cache_root,
                artifact=verified_model_artifact,
                split_id=split_id,
                inference_config=inference_config,
                inference_config_hash=inference_config_hash,
                now=now,
            )
    except BenchmarkArtifactUnavailableError as exc:
        run = _unavailable_run(
            candidate=candidate, dataset=dataset, split_id=split_id, reason=str(exc), now=now
        )

    reject_private_local_paths(run.model_dump(mode="json"))
    _write_result(run, resolved_output_root)
    return run


def _run_detection(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    artifact: VerifiedModelArtifact,
    split_id: SplitId,
    inference_config: dict[str, object],
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = load_detection_benchmark_manifest(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    if model_cache_root is None:
        raise BenchmarkArtifactUnavailableError(
            "a model cache root is required for a real detection benchmark run"
        )
    variant = "nano" if candidate.candidate_id.endswith("n") else "small"
    try:
        engine = _build_real_detector_engine(
            model_cache_root=model_cache_root, artifact=artifact, variant=variant
        )
    except EngineError as exc:
        raise BenchmarkArtifactUnavailableError(str(exc)) from exc
    return run_detection_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config=inference_config,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _run_tracking(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    artifact: VerifiedModelArtifact,
    split_id: SplitId,
    inference_config: dict[str, object],
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = load_tracking_benchmark_manifest(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    try:
        engine = _build_real_tracker_engine(artifact=artifact)
    except EngineError as exc:
        raise BenchmarkArtifactUnavailableError(str(exc)) from exc
    return run_tracking_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config=inference_config,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _run_visual_text(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    artifact: VerifiedModelArtifact,
    split_id: SplitId,
    inference_config: dict[str, object],
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = load_visual_text_benchmark_manifest(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    if model_cache_root is None:
        raise BenchmarkArtifactUnavailableError(
            "a model cache root is required for a real visual-text benchmark run"
        )
    try:
        engine = _build_real_visual_text_engine(
            model_cache_root=model_cache_root, artifact=artifact
        )
    except EngineError as exc:
        raise BenchmarkArtifactUnavailableError(str(exc)) from exc
    return run_visual_text_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config=inference_config,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _write_result(run: BenchmarkRunV1, output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    result_path = output_root / f"{run.run_id}.json"
    result_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")


__all__ = [
    "VerifiedModelArtifact",
    "dataset_subdirectory",
    "run_benchmark",
]
