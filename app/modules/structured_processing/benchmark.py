"""Phase 7 Part 2 benchmark orchestration: the one entry point the CLI calls.

`run_benchmark` validates the requested dataset/candidate against Part 1's
frozen catalogue and this task's own Part 2 scope, resolves local roots
from explicit configuration only, checks artifact availability *before*
attempting to execute anything, dispatches to the right adapter
(`benchmark_adapters.py`), and writes a safe aggregate JSON result. It
never downloads anything, never writes to Neo4j, and never fabricates a
`succeeded` result for a candidate/dataset combination whose local
artifacts are missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.evaluation.models import (
    BenchmarkRunStatus,
    BenchmarkRunV1,
    DatasetManifestEntryV1,
    ModelCandidateV1,
    SplitId,
)
from app.modules.structured_processing.benchmark_adapters import (
    RUNTIME_ENVIRONMENT,
    BenchmarkArtifactUnavailableError,
    ConfiguredOcrEngine,
    OcrEngine,
    OcrEngineError,
    RealOcrEngineConfig,
    discover_single_input_file,
    load_ocr_benchmark_manifest,
    run_ocr_benchmark,
    run_structured_benchmark,
)
from app.modules.structured_processing.benchmark_validation import (
    reject_private_local_paths,
    resolve_benchmark_data_root,
    resolve_benchmark_output_root,
    resolve_model_cache_root,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)

#: Every `local_path_placeholder` in the frozen manifest starts with this
#: repo-relative prefix (see `evaluation.validation.ALLOWED_LOCAL_DATA_PREFIXES`);
#: stripping it lets `TRACEX_BENCHMARK_DATA_ROOT` point anywhere on a real
#: machine (a MacBook's own dataset drive, say) while the manifest's own
#: placeholder stays a portable, dataset-relative suffix.
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
class OcrCandidateArtifact:
    """Verified-during-MacBook-pre-flight PaddleOCR model metadata.

    Every field must come from an artifact Aditya's pre-flight actually
    inspected -- this module never invents a model name, version, or hash.
    """

    model_name: str
    model_version: str
    model_sha256: str


def _build_paddleocr_engine(
    *, model_cache_root: Path, artifact: OcrCandidateArtifact, variant: str
) -> OcrEngine:
    """Best-effort real PaddleOCR wiring.

    This session cannot install or exercise PaddleOCR, so the exact
    `paddleocr` Python API called here is written from its documented
    public shape and may need a small adjustment once Aditya's MacBook
    pre-flight confirms the actually-installed version's API -- any
    mismatch degrades to a safe `BenchmarkArtifactUnavailableError`
    (caught by `run_benchmark` into an `UNAVAILABLE` result), never a
    crash or a fabricated result. See `docs/runbooks/local-development.md`.
    """
    try:
        import paddleocr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "paddleocr is not installed in this environment -- run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md before a real "
            "PaddleOCR benchmark run"
        ) from exc

    try:
        weights_dir = model_cache_root / variant
        engine = paddleocr.PaddleOCR(det_model_dir=str(weights_dir), rec_model_dir=str(weights_dir))

        def recognize_fn(image_bytes: bytes) -> str:
            import io

            from PIL import Image  # already a transitive dependency via pypdfium2/Pillow

            image = Image.open(io.BytesIO(image_bytes))
            result = engine.ocr(image, cls=True)
            lines = [line[1][0] for page in (result or []) for line in (page or [])]
            return "\n".join(lines)

    except Exception as exc:  # noqa: BLE001 - any construction failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not construct the local PaddleOCR engine with the configured "
            "model cache -- verify the installed paddleocr API matches this "
            "adapter during MacBook pre-flight"
        ) from exc

    return ConfiguredOcrEngine(
        RealOcrEngineConfig(
            model_name=artifact.model_name,
            model_version=artifact.model_version,
            model_sha256=artifact.model_sha256,
            backend_label=f"paddleocr-{variant}-cpu",
            recognize_fn=recognize_fn,
        )
    )


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
        runtime_environment=RUNTIME_ENVIRONMENT,
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
    ocr_candidate_artifact: OcrCandidateArtifact | None = None,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    """Validate, resolve artifacts, run one benchmark, and write a safe result.

    Never raises for a missing local artifact -- that becomes a returned
    `BenchmarkRunStatus.UNAVAILABLE` result. Still raises for a genuinely
    invalid request (`UnknownDatasetError`/`UnknownCandidateError`/
    `UnsupportedCombinationError`/`BenchmarkConfigError`): those are caller
    bugs (a typo'd ID, a missing environment variable), not benchmark
    outcomes.
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

    if dataset.dataset_id == "fir_icdar_2023":
        run = _run_ocr(
            dataset=dataset,
            candidate=candidate,
            dataset_dir=dataset_dir,
            model_cache_root=resolved_model_cache_root,
            ocr_candidate_artifact=ocr_candidate_artifact,
            split_id=split_id,
            now=now,
        )
    else:
        run = _run_structured(
            dataset=dataset,
            candidate=candidate,
            dataset_dir=dataset_dir,
            split_id=split_id,
            now=now,
        )

    reject_private_local_paths(run.model_dump(mode="json"))
    _write_result(run, resolved_output_root)
    return run


def _run_ocr(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    ocr_candidate_artifact: OcrCandidateArtifact | None,
    split_id: SplitId,
    now: datetime,
) -> BenchmarkRunV1:
    try:
        samples = load_ocr_benchmark_manifest(dataset_dir)
    except BenchmarkArtifactUnavailableError as exc:
        return _unavailable_run(
            candidate=candidate, dataset=dataset, split_id=split_id, reason=str(exc), now=now
        )
    if not samples:
        return _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="benchmark manifest named zero usable samples",
            now=now,
        )

    variant = "mobile" if candidate.candidate_id.endswith("mobile") else "server"
    if model_cache_root is None or ocr_candidate_artifact is None:
        return _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="model cache root and verified model artifact metadata are both "
            "required for a real PaddleOCR run",
            now=now,
        )
    try:
        engine = _build_paddleocr_engine(
            model_cache_root=model_cache_root, artifact=ocr_candidate_artifact, variant=variant
        )
    except (BenchmarkArtifactUnavailableError, OcrEngineError) as exc:
        return _unavailable_run(
            candidate=candidate, dataset=dataset, split_id=split_id, reason=str(exc), now=now
        )

    return run_ocr_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config={
            "candidate_id": candidate.candidate_id,
            "model_name": ocr_candidate_artifact.model_name,
            "model_version": ocr_candidate_artifact.model_version,
        },
        now=now,
    )


def _run_structured(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    split_id: SplitId,
    now: datetime,
) -> BenchmarkRunV1:
    if not dataset_dir.is_dir():
        return _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="configured dataset directory does not exist locally",
            now=now,
        )
    input_file = discover_single_input_file(dataset_dir)
    if input_file is None:
        return _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="expected exactly one CSV/XLSX/JSON input file in the dataset "
            "directory and found zero or more than one",
            now=now,
        )
    task = "cdr" if dataset.dataset_id == "gomask_voice_cdr" else "finance"
    return run_structured_benchmark(
        task=task,
        input_path=input_file,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config={"candidate_id": candidate.candidate_id, "task": task},
        now=now,
    )


def _write_result(run: BenchmarkRunV1, output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    result_path = output_root / f"{run.run_id}.json"
    result_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")


__all__ = [
    "OcrCandidateArtifact",
    "dataset_subdirectory",
    "run_benchmark",
]
