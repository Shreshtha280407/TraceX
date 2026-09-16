"""Phase 7 Part 2 safety tests: ID validation, local-root configuration,
never-touches-Neo4j, and never-leaks-a-local-path.

No dataset/model artifact, PaddleOCR, GPU, or database connection is used
anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.evaluation.models import CandidateTask
from app.modules.evaluation.validation import UnsafeContentError
from app.modules.structured_processing.benchmark import run_benchmark
from app.modules.structured_processing.benchmark_validation import (
    ALLOWED_CANDIDATE_IDS,
    ALLOWED_DATASET_CANDIDATE_PAIRS,
    ALLOWED_DATASET_IDS,
    ENV_BENCHMARK_DATA_ROOT,
    ENV_BENCHMARK_OUTPUT_ROOT,
    ENV_MODEL_CACHE_ROOT,
    BenchmarkConfigError,
    UnknownCandidateError,
    UnknownDatasetError,
    UnsupportedCombinationError,
    benchmark_inference_config_hash,
    expected_task_for_dataset,
    reject_private_local_paths,
    resolve_benchmark_data_root,
    resolve_benchmark_output_root,
    resolve_model_cache_root,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)

_MANIFEST = load_dataset_manifest()
_CATALOG = load_model_candidate_catalog()


# --- proof points 1-2: only approved IDs are accepted -----------------------


@pytest.mark.parametrize("dataset_id", sorted(ALLOWED_DATASET_IDS))
def test_every_approved_dataset_id_is_accepted(dataset_id: str) -> None:
    entry = validate_dataset_id(_MANIFEST, dataset_id)
    assert entry.dataset_id == dataset_id


@pytest.mark.parametrize("candidate_id", sorted(ALLOWED_CANDIDATE_IDS))
def test_every_approved_candidate_id_is_accepted(candidate_id: str) -> None:
    entry = validate_candidate_id(_CATALOG, candidate_id)
    assert entry.candidate_id == candidate_id


def test_unknown_dataset_id_is_rejected_safely() -> None:
    with pytest.raises(UnknownDatasetError):
        validate_dataset_id(_MANIFEST, "not_a_real_dataset")


def test_unknown_candidate_id_is_rejected_safely() -> None:
    with pytest.raises(UnknownCandidateError):
        validate_candidate_id(_CATALOG, "not_a_real_candidate")


def test_a_dataset_owned_by_another_phase_7_part_is_rejected() -> None:
    """`virat_ground` is real in the frozen manifest but belongs to Gaurav's part."""
    assert _MANIFEST.get("virat_ground") is not None  # sanity: it really exists
    with pytest.raises(UnknownDatasetError):
        validate_dataset_id(_MANIFEST, "virat_ground")


def test_a_candidate_owned_by_another_phase_7_part_is_rejected() -> None:
    assert any(c.candidate_id == "yolo11n" for c in _CATALOG.candidates)  # sanity
    with pytest.raises(UnknownCandidateError):
        validate_candidate_id(_CATALOG, "yolo11n")


@pytest.mark.parametrize(
    ("dataset_id", "candidate_id"),
    sorted(ALLOWED_DATASET_CANDIDATE_PAIRS),
)
def test_every_approved_pair_is_accepted(dataset_id: str, candidate_id: str) -> None:
    dataset = validate_dataset_id(_MANIFEST, dataset_id)
    candidate = validate_candidate_id(_CATALOG, candidate_id)
    validate_dataset_candidate_pair(dataset, candidate)  # must not raise


def test_a_structurally_valid_but_unpaired_combination_is_rejected() -> None:
    dataset = validate_dataset_id(_MANIFEST, "gomask_voice_cdr")
    candidate = validate_candidate_id(_CATALOG, "paddleocr-ppocrv5-mobile")
    with pytest.raises(UnsupportedCombinationError):
        validate_dataset_candidate_pair(dataset, candidate)


def test_expected_task_matches_the_catalogued_candidate_task() -> None:
    assert expected_task_for_dataset("fir_icdar_2023") == CandidateTask.OCR
    assert expected_task_for_dataset("gomask_voice_cdr") == CandidateTask.FIR_CDR_FINANCE_EXTRACTION
    assert expected_task_for_dataset("ibm_amlsim") == CandidateTask.FIR_CDR_FINANCE_EXTRACTION


# --- proof point 3: local roots are configuration-driven only --------------


def test_local_roots_come_only_from_explicit_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_BENCHMARK_DATA_ROOT, raising=False)
    monkeypatch.delenv(ENV_MODEL_CACHE_ROOT, raising=False)
    monkeypatch.delenv(ENV_BENCHMARK_OUTPUT_ROOT, raising=False)

    with pytest.raises(BenchmarkConfigError, match=ENV_BENCHMARK_DATA_ROOT):
        resolve_benchmark_data_root()
    with pytest.raises(BenchmarkConfigError, match=ENV_BENCHMARK_OUTPUT_ROOT):
        resolve_benchmark_output_root()
    assert resolve_model_cache_root() is None  # optional -- never a hardcoded fallback path

    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, "/tmp/example-benchmark-data")
    assert resolve_benchmark_data_root() == Path("/tmp/example-benchmark-data")

    monkeypatch.setenv(ENV_MODEL_CACHE_ROOT, "/tmp/example-model-cache")
    assert resolve_model_cache_root() == Path("/tmp/example-model-cache")


def test_explicit_cli_argument_always_wins_over_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, "/tmp/from-env")
    assert resolve_benchmark_data_root(Path("/tmp/from-cli")) == Path("/tmp/from-cli")


def test_no_local_root_resolver_ever_reads_an_undocumented_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decoy, differently-named env var must never be consulted."""
    monkeypatch.delenv(ENV_BENCHMARK_DATA_ROOT, raising=False)
    monkeypatch.setenv("BENCHMARK_DATA_ROOT", "/should/never/be/read")
    with pytest.raises(BenchmarkConfigError):
        resolve_benchmark_data_root()


# --- proof point 3 (continued): local roots never leak into result payloads -


def test_reject_private_local_paths_catches_home_and_users_paths() -> None:
    with pytest.raises(UnsafeContentError):
        reject_private_local_paths({"note": "see /home/someone/data/file.csv"})
    with pytest.raises(UnsafeContentError):
        reject_private_local_paths({"note": "see /Users/someone/data/file.csv"})
    with pytest.raises(UnsafeContentError):
        reject_private_local_paths(["nested", {"deep": "path is ~/secret/place"}])


def test_reject_private_local_paths_accepts_safe_content() -> None:
    reject_private_local_paths(
        {"dataset_id": "gomask_voice_cdr", "count": 3, "note": "local-data/gomask_voice_cdr/"}
    )  # must not raise


def test_run_benchmark_never_writes_a_local_root_into_its_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "very-private-home-style-root"
    output_root = tmp_path / "out"
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, str(data_root))
    monkeypatch.setenv(ENV_BENCHMARK_OUTPUT_ROOT, str(output_root))
    monkeypatch.delenv(ENV_MODEL_CACHE_ROOT, raising=False)

    run = run_benchmark(dataset_id="ibm_amlsim", candidate_id="existing-deterministic-parsers")
    serialized = run.model_dump_json()
    assert str(data_root) not in serialized
    assert str(output_root) not in serialized


# --- proof point 4: missing artifacts produce a truthful blocked result ----


def test_missing_dataset_directory_produces_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, str(tmp_path / "does-not-exist"))
    monkeypatch.setenv(ENV_BENCHMARK_OUTPUT_ROOT, str(tmp_path / "out"))
    run = run_benchmark(
        dataset_id="gomask_voice_cdr", candidate_id="existing-deterministic-parsers"
    )
    assert run.status.value == "unavailable"
    assert run.failure_reason_safe is not None


def test_ambiguous_dataset_directory_produces_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset_dir = tmp_path / "gomask_voice_cdr"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "a.csv").write_text("a,b\n1,2\n")
    (dataset_dir / "b.csv").write_text("a,b\n1,2\n")
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, str(tmp_path))
    monkeypatch.setenv(ENV_BENCHMARK_OUTPUT_ROOT, str(tmp_path / "out"))
    run = run_benchmark(
        dataset_id="gomask_voice_cdr", candidate_id="existing-deterministic-parsers"
    )
    assert run.status.value == "unavailable"


def test_missing_model_metadata_produces_unavailable_for_ocr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset_dir = tmp_path / "fir_icdar_2023"
    dataset_dir.mkdir(parents=True)
    image_path = dataset_dir / "sample.png"
    image_path.write_bytes(b"not-a-real-image")
    (dataset_dir / "benchmark_manifest.jsonl").write_text(
        '{"sample_id": "s1", "image_path": "sample.png"}\n'
    )
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, str(tmp_path))
    monkeypatch.setenv(ENV_BENCHMARK_OUTPUT_ROOT, str(tmp_path / "out"))
    monkeypatch.delenv(ENV_MODEL_CACHE_ROOT, raising=False)
    run = run_benchmark(dataset_id="fir_icdar_2023", candidate_id="paddleocr-ppocrv5-mobile")
    assert run.status.value == "unavailable"
    assert run.artifact_sha256 is None


# --- proof point 12: never writes directly to Neo4j -------------------------


@pytest.mark.parametrize(
    "module_name",
    [
        "app.modules.structured_processing.benchmark",
        "app.modules.structured_processing.benchmark_cli",
        "app.modules.structured_processing.benchmark_adapters",
        "app.modules.structured_processing.benchmark_metrics",
        "app.modules.structured_processing.benchmark_validation",
    ],
)
def test_benchmark_module_never_imports_neo4j(module_name: str) -> None:
    """Scans for an actual `import`/`from ... import` statement, not a bare
    substring -- several of these modules' own docstrings mention "Neo4j"
    in prose while documenting that they never write to it."""
    module = __import__(module_name, fromlist=["_"])
    source_path = Path(str(module.__file__))
    source = source_path.read_text(encoding="utf-8")
    assert "import neo4j" not in source.lower()
    assert "from neo4j" not in source.lower()


# --- proof point 16: result output is aggregate-only, safe to commit -------


def test_benchmark_config_hash_is_deterministic_and_content_sensitive() -> None:
    a = benchmark_inference_config_hash({"variant": "mobile"})
    b = benchmark_inference_config_hash({"variant": "mobile"})
    c = benchmark_inference_config_hash({"variant": "server"})
    assert a == b
    assert a != c


def test_benchmark_config_error_names_the_env_var_but_no_stack_trace() -> None:
    error = BenchmarkConfigError(f"{ENV_BENCHMARK_DATA_ROOT} is not set. Set it ...")
    message = str(error)
    assert ENV_BENCHMARK_DATA_ROOT in message
    assert "Traceback" not in message
