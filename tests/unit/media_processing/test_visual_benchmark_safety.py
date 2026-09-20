"""Phase 7 Part 3 visual-benchmark safety tests.

Covers: dataset/candidate allow-listing, the licence-clearance execution
gate, private-path rejection, forbidden metric-key rejection, the frozen
`BenchmarkRunV1` artifact-hash requirement, config-hash determinism, and a
static "no direct infrastructure write" check across this task's own new
modules. Every fixture here is synthetic and constructed directly -- never
loaded from a real downloaded dataset or model.
"""

from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.evaluation.models import (
    BenchmarkRunStatus,
    BenchmarkRunV1,
    CandidateSelectionStatus,
    CandidateTask,
    DatasetEvaluationStatus,
    DatasetManifestEntryV1,
    DatasetRole,
    EvaluationModality,
    ExecutionTarget,
    GitStoragePolicy,
    LicenseStatus,
    ModelCandidateV1,
    RedistributionStatus,
    SplitId,
    TeamOwner,
)
from app.modules.media_processing import (
    visual_benchmark,
    visual_benchmark_adapters,
    visual_benchmark_cli,
    visual_benchmark_metrics,
    visual_benchmark_validation,
)
from app.modules.media_processing.visual_benchmark_validation import (
    ALLOWED_CANDIDATE_IDS,
    ALLOWED_DATASET_CANDIDATE_PAIRS,
    ALLOWED_DATASET_IDS,
    BenchmarkConfigError,
    LicenseNotClearedError,
    UnknownCandidateError,
    UnknownDatasetError,
    UnsupportedCombinationError,
    benchmark_inference_config_hash,
    reject_private_local_paths,
    require_license_cleared_for_real_execution,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _dataset(
    *,
    dataset_id: str = "virat_ground",
    license_status: LicenseStatus = LicenseStatus.PENDING_VERIFICATION,
) -> DatasetManifestEntryV1:
    return DatasetManifestEntryV1(
        schema_version="v1",
        dataset_id=dataset_id,
        display_name="Example Dataset",
        role=DatasetRole.PRIMARY_BENCHMARK,
        owner=TeamOwner.GAURAV,
        modalities=(EvaluationModality.VIDEO,),
        allowed_tasks=("example task",),
        prohibited_claims=("must not be used for identity",),
        source_reference="example source",
        release_or_version="pending_verification",
        license_status=license_status,
        license_notes="example notes",
        local_path_placeholder=f"local-data/{dataset_id}/",
        git_storage_policy=GitStoragePolicy.EXCLUDED_MANIFEST_ONLY,
        redistribution_status=RedistributionStatus.PENDING_VERIFICATION,
        evaluation_status=DatasetEvaluationStatus.LICENSE_VERIFICATION_REQUIRED,
        known_limitations=("example limitation",),
    )


def _candidate(
    *,
    candidate_id: str = "yolo11n",
    task: CandidateTask = CandidateTask.DETECTION,
    license_status: LicenseStatus = LicenseStatus.PENDING_VERIFICATION,
) -> ModelCandidateV1:
    return ModelCandidateV1(
        schema_version="v1",
        candidate_id=candidate_id,
        owner=TeamOwner.GAURAV,
        task=task,
        framework="Example",
        model_family="ExampleFamily",
        variant="example",
        execution_target=ExecutionTarget.CPU,
        license_status=license_status,
        source_reference="example source",
        selection_status=CandidateSelectionStatus.CANDIDATE,
        required_metrics=("latency_ms",),
        known_limitations=("example limitation",),
    )


def _run(**overrides: object) -> BenchmarkRunV1:
    defaults: dict[str, object] = {
        "schema_version": "v1",
        "run_id": "virat_ground-yolo11n-abc123456789",
        "candidate_id": "yolo11n",
        "dataset_id": "virat_ground",
        "split_id": SplitId.DEVELOPMENT,
        "task": CandidateTask.DETECTION,
        "runtime_environment": "phase7-part3-visual-benchmark-cli-v1",
        "hardware_profile": "cpu",
        "inference_config_hash": "a" * 64,
        "artifact_sha256": "b" * 64,
        "metrics": {"precision": 1.0, "latency_ms": 5.0},
        "started_at": _NOW,
        "completed_at": _NOW,
        "status": BenchmarkRunStatus.SUCCEEDED,
        "failure_reason_safe": None,
    }
    defaults.update(overrides)
    return BenchmarkRunV1(**defaults)  # type: ignore[arg-type]


# --- Allow-listing -----------------------------------------------------


@pytest.mark.parametrize("dataset_id", sorted(ALLOWED_DATASET_IDS))
def test_every_approved_dataset_id_is_accepted(dataset_id: str) -> None:
    manifest = load_dataset_manifest()
    entry = validate_dataset_id(manifest, dataset_id)
    assert entry.dataset_id == dataset_id


@pytest.mark.parametrize("candidate_id", sorted(ALLOWED_CANDIDATE_IDS))
def test_every_approved_candidate_id_is_accepted(candidate_id: str) -> None:
    catalog = load_model_candidate_catalog()
    candidate = validate_candidate_id(catalog, candidate_id)
    assert candidate.candidate_id == candidate_id


def test_unknown_dataset_id_is_rejected_safely() -> None:
    manifest = load_dataset_manifest()
    with pytest.raises(UnknownDatasetError):
        validate_dataset_id(manifest, "not_a_real_dataset")


def test_unknown_candidate_id_is_rejected_safely() -> None:
    catalog = load_model_candidate_catalog()
    with pytest.raises(UnknownCandidateError):
        validate_candidate_id(catalog, "not_a_real_candidate")


def test_a_dataset_owned_by_another_phase_7_part_is_rejected() -> None:
    manifest = load_dataset_manifest()
    with pytest.raises(UnknownDatasetError):
        validate_dataset_id(manifest, "fir_icdar_2023")


def test_a_candidate_owned_by_another_phase_7_part_is_rejected() -> None:
    catalog = load_model_candidate_catalog()
    with pytest.raises(UnknownCandidateError):
        validate_candidate_id(catalog, "faster-whisper-small")


@pytest.mark.parametrize("dataset_id,candidate_id", sorted(ALLOWED_DATASET_CANDIDATE_PAIRS))
def test_every_approved_pair_is_accepted(dataset_id: str, candidate_id: str) -> None:
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, dataset_id)
    candidate = validate_candidate_id(catalog, candidate_id)
    validate_dataset_candidate_pair(dataset, candidate)  # must not raise


def test_a_structurally_valid_but_unpaired_combination_is_rejected() -> None:
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, "safe_unsafe_behaviour")
    candidate = validate_candidate_id(catalog, "bytetrack")
    with pytest.raises(UnsupportedCombinationError):
        validate_dataset_candidate_pair(dataset, candidate)


# --- Licence-clearance execution gate -----------------------------------


def test_pending_verification_dataset_blocks_a_real_run() -> None:
    dataset = _dataset(license_status=LicenseStatus.PENDING_VERIFICATION)
    candidate = _candidate(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    with pytest.raises(LicenseNotClearedError, match="virat_ground"):
        require_license_cleared_for_real_execution(dataset, candidate)


def test_pending_verification_candidate_blocks_a_real_run() -> None:
    dataset = _dataset(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    candidate = _candidate(license_status=LicenseStatus.PENDING_VERIFICATION)
    with pytest.raises(LicenseNotClearedError, match="yolo11n"):
        require_license_cleared_for_real_execution(dataset, candidate)


def test_verified_dataset_and_candidate_clear_the_gate() -> None:
    dataset = _dataset(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    candidate = _candidate(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    require_license_cleared_for_real_execution(dataset, candidate)  # must not raise


def test_every_real_gaurav_dataset_and_candidate_licence_status_is_documented() -> None:
    """Documents today's actual state after Gate B's real verification.

    `virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` are now genuinely
    licence-cleared (Gate B read the real VIRAT Usage Agreement and the
    real Ultralytics AGPL-3.0 licence directly). `safe_unsafe_behaviour`
    and `ufpr_alpr` remain `pending_verification` -- both are legitimately
    deferred (no pinned source for the former; an academic access-request
    gate for the latter), not silently skipped.
    """
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    cleared_datasets = {"virat_ground"}
    still_pending_datasets = {"safe_unsafe_behaviour", "ufpr_alpr"}
    assert cleared_datasets | still_pending_datasets == ALLOWED_DATASET_IDS
    for dataset_id in cleared_datasets:
        dataset = validate_dataset_id(manifest, dataset_id)
        assert dataset.license_status != LicenseStatus.PENDING_VERIFICATION
    for dataset_id in still_pending_datasets:
        dataset = validate_dataset_id(manifest, dataset_id)
        assert dataset.license_status == LicenseStatus.PENDING_VERIFICATION

    cleared_candidates = {"yolo11n", "yolo11s", "bytetrack"}
    still_pending_candidates = {"paddleocr-lightweight-visual-text"}
    assert cleared_candidates | still_pending_candidates == ALLOWED_CANDIDATE_IDS
    for candidate_id in cleared_candidates:
        candidate = validate_candidate_id(catalog, candidate_id)
        assert candidate.license_status != LicenseStatus.PENDING_VERIFICATION
    for candidate_id in still_pending_candidates:
        candidate = validate_candidate_id(catalog, candidate_id)
        assert candidate.license_status == LicenseStatus.PENDING_VERIFICATION


def test_virat_ground_and_yolo_candidates_now_clear_the_licence_gate() -> None:
    """The genuinely discovered, real-verified pair this Gate B run produced
    actual `SUCCEEDED` results for -- confirmed by a real local CLI run
    against a real downloaded clip during this task's own verification
    (see `docs/qa/test-results.md`).
    """
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, "virat_ground")
    for candidate_id in ("yolo11n", "yolo11s", "bytetrack"):
        candidate = validate_candidate_id(catalog, candidate_id)
        require_license_cleared_for_real_execution(dataset, candidate)  # must not raise


# --- Artifact-hash requirement (frozen Part 1 BenchmarkRunV1 validator) --


def test_a_completed_result_without_an_artifact_hash_is_rejected() -> None:
    with pytest.raises(ValidationError, match="artifact_sha256"):
        _run(artifact_sha256=None)


def test_a_completed_result_with_a_valid_artifact_hash_is_accepted() -> None:
    run = _run()
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.artifact_sha256 is not None


# --- Forbidden content in metrics and free text --------------------------


def test_a_metric_key_shaped_like_a_real_identifier_is_rejected() -> None:
    with pytest.raises(ValidationError, match="prohibited"):
        _run(metrics={"phone_number": 42.0})


def test_reject_private_local_paths_catches_home_and_users_paths() -> None:
    with pytest.raises(BenchmarkConfigError):
        reject_private_local_paths({"note": "/home/gaurav/data/virat_ground/clip1.mp4"})
    with pytest.raises(BenchmarkConfigError):
        reject_private_local_paths({"note": "/Users/aditya/model-cache/yolo11n"})


def test_reject_private_local_paths_accepts_safe_content() -> None:
    reject_private_local_paths(
        {"dataset_id": "virat_ground", "metrics": {"precision": 1.0}, "note": "local-data/x/"}
    )  # must not raise


def test_run_benchmark_result_never_contains_a_private_local_path(tmp_path: Path) -> None:
    result = visual_benchmark.run_benchmark(
        dataset_id="virat_ground",
        candidate_id="yolo11n",
        data_root=tmp_path / "data",
        output_root=tmp_path / "out",
    )
    serialized = result.model_dump_json()
    assert str(tmp_path) not in serialized


# --- Determinism -----------------------------------------------------------


def test_benchmark_config_hash_is_deterministic_and_content_sensitive() -> None:
    config_a = {"candidate_id": "yolo11n", "dataset_id": "virat_ground"}
    config_b = {"dataset_id": "virat_ground", "candidate_id": "yolo11n"}  # same content, reordered
    config_c = {"candidate_id": "yolo11s", "dataset_id": "virat_ground"}
    assert benchmark_inference_config_hash(config_a) == benchmark_inference_config_hash(config_b)
    assert benchmark_inference_config_hash(config_a) != benchmark_inference_config_hash(config_c)


# --- No direct infrastructure write (static source scan) ------------------


@pytest.mark.parametrize(
    "module",
    [
        visual_benchmark,
        visual_benchmark_adapters,
        visual_benchmark_cli,
        visual_benchmark_metrics,
        visual_benchmark_validation,
    ],
)
def test_visual_benchmark_module_never_imports_a_database_or_object_store_client(
    module: ModuleType,
) -> None:
    source = inspect.getsource(module).lower()
    for forbidden in (
        "import neo4j",
        "from neo4j",
        "import asyncpg",
        "from asyncpg",
        "import psycopg",
        "from psycopg",
        "import minio",
        "from minio",
        "import sqlalchemy",
        "from sqlalchemy",
    ):
        assert forbidden not in source, f"{module.__name__} unexpectedly references {forbidden!r}"


def test_run_benchmark_never_writes_a_local_root_string_into_the_result(tmp_path: Path) -> None:
    data_root = tmp_path / "some-private-data-root"
    output_root = tmp_path / "out"
    result = visual_benchmark.run_benchmark(
        dataset_id="ufpr_alpr", candidate_id="yolo11n", data_root=data_root, output_root=output_root
    )
    assert str(data_root) not in result.model_dump_json()


# --- Real-engine builders degrade to UNAVAILABLE only for a missing -------
# --- optional runtime, never because the integration itself is unfinished -


def _artifact() -> visual_benchmark.VerifiedModelArtifact:
    return visual_benchmark.VerifiedModelArtifact(
        model_name="example", model_version="example-1", model_sha256="a" * 64
    )


def _force_import_error(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Forces `import <module_name>` to raise `ImportError`, even if the real
    package is genuinely installed in this environment (as it is on a real
    Gate B host that has run `uv sync --extra video-benchmark`). Setting a
    `None` entry in `sys.modules` is documented CPython behaviour for
    exactly this -- it does not require the package to be absent, so this
    test's meaning does not depend on which packages happen to be installed
    wherever it runs.
    """
    monkeypatch.setitem(sys.modules, module_name, None)


def test_real_detector_engine_is_unavailable_without_ultralytics_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_import_error(monkeypatch, "ultralytics")
    with pytest.raises(
        visual_benchmark_adapters.BenchmarkArtifactUnavailableError, match="ultralytics"
    ):
        visual_benchmark._build_real_detector_engine(
            model_cache_root=tmp_path, artifact=_artifact(), variant="nano"
        )


def test_real_tracker_engine_is_unavailable_without_ultralytics_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ByteTrack integration itself is complete -- see visual_benchmark.py's
    `_build_real_tracker_engine` -- this proves it degrades safely only
    because `ultralytics` is unavailable, not because the wiring was left
    unfinished.
    """
    _force_import_error(monkeypatch, "ultralytics")
    with pytest.raises(
        visual_benchmark_adapters.BenchmarkArtifactUnavailableError, match="ultralytics"
    ):
        visual_benchmark._build_real_tracker_engine(artifact=_artifact())


def test_real_visual_text_engine_is_unavailable_without_paddleocr_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_import_error(monkeypatch, "paddleocr")
    with pytest.raises(
        visual_benchmark_adapters.BenchmarkArtifactUnavailableError, match="paddleocr"
    ):
        visual_benchmark._build_real_visual_text_engine(
            model_cache_root=tmp_path, artifact=_artifact()
        )


# --- Real Ultralytics ByteTrack wiring: genuine Gate B regression coverage -
#
# These self-skip (never fabricate a pass) if `ultralytics` is not actually
# installed -- exactly the "no live infra needed for the rest of this file"
# convention this module's own docstring describes, extended here because
# Gate B genuinely installed `ultralytics` and found two real API-drift bugs
# a mock could not have caught: `ultralytics.utils.yaml_load` no longer
# exists (replaced by `YAML.load`), and `BYTETracker.__init__` no longer
# accepts a `frame_rate` keyword argument at all.


def test_real_tracker_engine_builds_successfully_when_ultralytics_is_installed() -> None:
    pytest.importorskip("ultralytics")
    engine = visual_benchmark._build_real_tracker_engine(artifact=_artifact())
    assert hasattr(engine, "track")
    assert callable(engine.track)


def test_real_tracker_engine_tracks_a_real_object_across_frames() -> None:
    """Regression test for the real Gate B bugs this module's own git
    history fixed: `YAML.load` replacing `yaml_load`, `BYTETracker`
    dropping its `frame_rate` parameter, and needing a real
    `ultralytics.engine.results.Boxes` instance (which supports numpy-style
    indexing) rather than a plain `SimpleNamespace` duck-type.
    """
    pytest.importorskip("ultralytics")
    from app.modules.media_processing.analysis.interfaces import ObjectDetection
    from app.modules.media_processing.image.geometry import PixelBoundingBox

    engine = visual_benchmark._build_real_tracker_engine(artifact=_artifact())
    detection = ObjectDetection(
        label="car",
        confidence=0.9,
        box=PixelBoundingBox(x_min=10.0, y_min=10.0, x_max=50.0, y_max=50.0),
    )
    detections_by_time_ms = {0: (detection,), 33: (detection,), 66: (detection,)}
    result = engine.track(detections_by_time_ms)
    assert len(result.tracks) >= 1
    assert result.backend == "cpu"
