"""Phase 7 Part 4 audio/social-benchmark safety tests.

Covers: dataset/candidate allow-listing, the licence/conditional-status
execution gate, private-path rejection, forbidden metric-key rejection,
the frozen `BenchmarkRunV1` artifact-hash requirement, config-hash
determinism, missing-optional-dependency safe degradation, and a static
"no direct infrastructure write" check across this task's own new
modules. Every fixture here is synthetic and constructed directly -- never
loaded from a real downloaded dataset or model.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from app.modules.communication_processing import (
    audio_social_benchmark,
    audio_social_benchmark_adapters,
    audio_social_benchmark_cli,
    audio_social_benchmark_metrics,
    audio_social_benchmark_validation,
)
from app.modules.communication_processing.audio_social_benchmark import VerifiedModelArtifact
from app.modules.communication_processing.audio_social_benchmark_validation import (
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
    require_cleared_for_real_execution,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)
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

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _dataset(
    *,
    dataset_id: str = "vast_social_text",
    license_status: LicenseStatus = LicenseStatus.PENDING_VERIFICATION,
) -> DatasetManifestEntryV1:
    return DatasetManifestEntryV1(
        schema_version="v1",
        dataset_id=dataset_id,
        display_name="Example Dataset",
        role=DatasetRole.PRIMARY_BENCHMARK,
        owner=TeamOwner.SARTHAK,
        modalities=(EvaluationModality.SOCIAL_TEXT,),
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
    candidate_id: str = "existing-deterministic-social-parsers",
    task: CandidateTask = CandidateTask.SOCIAL_TEXT_EXTRACTION,
    license_status: LicenseStatus = LicenseStatus.PENDING_VERIFICATION,
    selection_status: CandidateSelectionStatus = CandidateSelectionStatus.CANDIDATE,
) -> ModelCandidateV1:
    return ModelCandidateV1(
        schema_version="v1",
        candidate_id=candidate_id,
        owner=TeamOwner.SARTHAK,
        task=task,
        framework="Example",
        model_family="ExampleFamily",
        variant="example",
        execution_target=ExecutionTarget.CPU,
        license_status=license_status,
        source_reference="example source",
        selection_status=selection_status,
        required_metrics=("latency_ms",),
        known_limitations=("example limitation",),
    )


def _run(**overrides: object) -> BenchmarkRunV1:
    defaults: dict[str, object] = {
        "schema_version": "v1",
        "run_id": "vast_social_text-existing-deterministic-social-parsers-abc123456789",
        "candidate_id": "existing-deterministic-social-parsers",
        "dataset_id": "vast_social_text",
        "split_id": SplitId.DEVELOPMENT,
        "task": CandidateTask.SOCIAL_TEXT_EXTRACTION,
        "runtime_environment": "phase7-part4-audio-social-benchmark-cli-v1",
        "hardware_profile": "cpu",
        "inference_config_hash": "a" * 64,
        "artifact_sha256": "b" * 64,
        "metrics": {"extraction_precision": 1.0, "latency_ms": 5.0},
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
        validate_dataset_id(manifest, "virat_ground")


def test_a_candidate_owned_by_another_phase_7_part_is_rejected() -> None:
    catalog = load_model_candidate_catalog()
    with pytest.raises(UnknownCandidateError):
        validate_candidate_id(catalog, "yolo11n")


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
    dataset = validate_dataset_id(manifest, "common_voice_indic")
    candidate = validate_candidate_id(catalog, "silero-vad-v6")
    with pytest.raises(UnsupportedCombinationError):
        validate_dataset_candidate_pair(dataset, candidate)


# --- Licence/conditional-status execution gate ---------------------------


def test_pending_verification_dataset_blocks_a_real_run() -> None:
    dataset = _dataset(license_status=LicenseStatus.PENDING_VERIFICATION)
    candidate = _candidate(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    with pytest.raises(LicenseNotClearedError, match="vast_social_text"):
        require_cleared_for_real_execution(dataset, candidate)


def test_pending_verification_candidate_blocks_a_real_run() -> None:
    dataset = _dataset(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    candidate = _candidate(license_status=LicenseStatus.PENDING_VERIFICATION)
    with pytest.raises(LicenseNotClearedError, match="existing-deterministic-social-parsers"):
        require_cleared_for_real_execution(dataset, candidate)


def test_conditional_candidate_blocks_a_real_run_even_with_cleared_licenses() -> None:
    dataset = _dataset(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    candidate = _candidate(
        license_status=LicenseStatus.VERIFIED_PERMISSIVE,
        selection_status=CandidateSelectionStatus.CONDITIONAL,
    )
    with pytest.raises(LicenseNotClearedError, match="conditional"):
        require_cleared_for_real_execution(dataset, candidate)


def test_verified_dataset_and_candidate_clear_the_gate() -> None:
    dataset = _dataset(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    candidate = _candidate(license_status=LicenseStatus.VERIFIED_PERMISSIVE)
    require_cleared_for_real_execution(dataset, candidate)  # must not raise


def test_vast_social_text_pair_is_already_license_cleared_today() -> None:
    """Documents a real, discovered asymmetry with Phase 7 Parts 2/3:
    unlike those parts' datasets (all `pending_verification`), two of
    Sarthak's four datasets are already `verified_permissive`, and the
    new deterministic social-parsers candidate is `internal_only` --
    together they clear this gate today, pending only real local data.
    """
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, "vast_social_text")
    candidate = validate_candidate_id(catalog, "existing-deterministic-social-parsers")
    require_cleared_for_real_execution(dataset, candidate)  # must not raise


def test_every_real_asr_and_diarization_candidate_is_currently_blocked() -> None:
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, "common_voice_indic")
    for candidate_id in ("faster-whisper-small", "faster-whisper-medium", "fasttext-lid176"):
        candidate = validate_candidate_id(catalog, candidate_id)
        with pytest.raises(LicenseNotClearedError):
            require_cleared_for_real_execution(dataset, candidate)


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
        reject_private_local_paths({"note": "/home/sarthak/data/common_voice_indic/clip1.wav"})
    with pytest.raises(BenchmarkConfigError):
        reject_private_local_paths({"note": "/Users/aditya/model-cache/faster-whisper-small"})


def test_reject_private_local_paths_accepts_safe_content() -> None:
    reject_private_local_paths(
        {
            "dataset_id": "vast_social_text",
            "metrics": {"extraction_precision": 1.0},
            "note": "local-data/x/",
        }
    )  # must not raise


def test_run_benchmark_result_never_contains_a_private_local_path(tmp_path: Path) -> None:
    result = audio_social_benchmark.run_benchmark(
        dataset_id="vast_social_text",
        candidate_id="existing-deterministic-social-parsers",
        data_root=tmp_path / "data",
        output_root=tmp_path / "out",
    )
    serialized = result.model_dump_json()
    assert str(tmp_path) not in serialized


# --- Determinism -----------------------------------------------------------


def test_benchmark_config_hash_is_deterministic_and_content_sensitive() -> None:
    config_a = {"candidate_id": "faster-whisper-small", "dataset_id": "common_voice_indic"}
    config_b = {"dataset_id": "common_voice_indic", "candidate_id": "faster-whisper-small"}
    config_c = {"candidate_id": "faster-whisper-medium", "dataset_id": "common_voice_indic"}
    assert benchmark_inference_config_hash(config_a) == benchmark_inference_config_hash(config_b)
    assert benchmark_inference_config_hash(config_a) != benchmark_inference_config_hash(config_c)


# --- Missing optional dependency -> safe unavailable, never a crash ------


def test_real_asr_engine_is_unavailable_without_faster_whisper_installed(tmp_path: Path) -> None:
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="faster_whisper"
    ):
        audio_social_benchmark._build_real_asr_engine(
            model_cache_root=tmp_path, artifact=artifact, variant="small"
        )


def test_real_diarization_engine_is_unavailable_without_pyannote_installed(tmp_path: Path) -> None:
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="pyannote"
    ):
        audio_social_benchmark._build_real_diarization_engine(
            model_cache_root=tmp_path, artifact=artifact
        )


def test_real_language_id_engine_is_unavailable_without_fasttext_installed(tmp_path: Path) -> None:
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="fasttext"
    ):
        audio_social_benchmark._build_real_language_id_engine(
            model_cache_root=tmp_path, artifact=artifact, transcription_stage_artifact=artifact
        )


def test_real_language_id_engine_is_unavailable_without_its_transcription_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fasttext-lid176` classifies text, not audio -- this harness chains a
    `faster_whisper` transcription stage in front of it (see
    `_build_real_language_id_engine`'s docstring). With `fasttext` faked as
    importable but `faster_whisper` genuinely absent, the function must still
    report `faster_whisper` as the missing piece, proving the second stage of
    the check is real and not skipped once the first import succeeds.
    """
    fake_fasttext = ModuleType("fasttext")
    monkeypatch.setitem(sys.modules, "fasttext", fake_fasttext)
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="faster_whisper"
    ):
        audio_social_benchmark._build_real_language_id_engine(
            model_cache_root=tmp_path, artifact=artifact, transcription_stage_artifact=artifact
        )


def test_language_id_result_requires_a_transcription_stage_artifact(tmp_path: Path) -> None:
    """A successful `fasttext-lid176` result requires both its own artifact
    reference *and* its chained transcription stage's -- the fastText
    reference alone is not enough, since fastText classifies the
    transcription stage's text output, never raw audio directly. Tests at
    the `_run_language_id` level (below the licence gate, which would
    otherwise short-circuit this candidate first) to exercise the
    artifact-requirement check itself.
    """
    dataset_dir = tmp_path / "data"
    dataset_dir.mkdir()
    (dataset_dir / "clip1.wav").write_bytes(b"never decoded before the artifact check fails")
    (dataset_dir / "benchmark_manifest.jsonl").write_text(
        json.dumps({"sample_id": "s1", "audio_path": "clip1.wav", "total_duration_ms": 1000}) + "\n"
    )
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, "common_voice_indic")
    candidate = validate_candidate_id(catalog, "fasttext-lid176")
    fasttext_artifact = VerifiedModelArtifact(
        model_name="fasttext-lid176", model_version="176", model_sha256="a" * 64
    )

    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError,
        match="transcription stage",
    ):
        audio_social_benchmark._run_language_id(
            dataset=dataset,
            candidate=candidate,
            dataset_dir=dataset_dir,
            model_cache_root=tmp_path / "cache",
            artifact=fasttext_artifact,
            transcription_stage_artifact=None,
            split_id=SplitId.DEVELOPMENT,
            inference_config_hash="0" * 64,
            now=datetime.now(UTC),
        )


def test_language_id_result_never_leaks_transcript_text_or_local_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcription stage's own transcript text is internal plumbing
    only -- it must never appear in the final `LanguageIdEngineResult`,
    alongside the usual "no local absolute path" and "no raw model bytes"
    invariants. Fakes both `fasttext` and `faster_whisper` (neither is
    installed here) with a distinctive marker transcript to prove it never
    escapes this function.
    """
    model_cache_root = tmp_path / "cache"
    lid_bytes = b"fake-fasttext-lid176-model-bytes"
    lid_path = model_cache_root / "lid.176.ftz"
    lid_path.parent.mkdir(parents=True)
    lid_path.write_bytes(lid_bytes)
    stage_dir = model_cache_root / "lid-transcription-stage"
    stage_dir.mkdir()
    stage_weight_bytes = b"fake-faster-whisper-transcription-stage-weights"
    (stage_dir / "model.bin").write_bytes(stage_weight_bytes)

    secret_transcript = "SUPER-SECRET-TRANSCRIPT-CONTENT-1234"

    class _FakeSegment:
        text = secret_transcript

    class _FakeWhisperModel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def transcribe(self, *args: object, **kwargs: object) -> tuple[list[_FakeSegment], object]:
            return [_FakeSegment()], object()

    class _FakeFastTextModel:
        def predict(self, text: str) -> tuple[list[str], list[float]]:
            assert secret_transcript in text
            return ["__label__hi"], [0.9]

    fake_fasttext = ModuleType("fasttext")
    fake_fasttext.load_model = lambda path: _FakeFastTextModel()  # noqa: ARG005
    monkeypatch.setitem(sys.modules, "fasttext", fake_fasttext)
    fake_faster_whisper = ModuleType("faster_whisper")
    fake_faster_whisper.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_faster_whisper)

    lid_artifact = VerifiedModelArtifact(
        model_name="fasttext-lid176",
        model_version="176",
        model_sha256=hashlib.sha256(lid_bytes).hexdigest(),
    )
    stage_artifact = VerifiedModelArtifact(
        model_name="faster-whisper-small",
        model_version="1",
        model_sha256=hashlib.sha256(stage_weight_bytes).hexdigest(),
    )
    engine = audio_social_benchmark._build_real_language_id_engine(
        model_cache_root=model_cache_root,
        artifact=lid_artifact,
        transcription_stage_artifact=stage_artifact,
    )
    result = engine.identify(b"fake-wav-bytes", filename="clip1.wav")
    assert result.predicted_language == "hi"

    serialized = json.dumps(
        {
            "predicted_language": result.predicted_language,
            "backend": result.backend,
            "model_name": result.model_name,
            "model_version": result.model_version,
            "model_sha256": result.model_sha256,
        }
    )
    assert secret_transcript not in serialized
    assert str(model_cache_root) not in serialized
    assert stage_weight_bytes.hex() not in serialized


def test_real_vad_engine_reports_unavailable_and_never_crashes(tmp_path: Path) -> None:
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="torch"
    ):
        audio_social_benchmark._build_real_vad_engine(model_cache_root=tmp_path, artifact=artifact)


# --- Real VAD wiring: offline-only, hash-verified, path-bounded ------------

_SILERO_WEIGHT_BYTES = b"fake-silero-vad-weights-for-testing-only"
_SILERO_WEIGHT_SHA256 = hashlib.sha256(_SILERO_WEIGHT_BYTES).hexdigest()


def _stage_silero_snapshot(model_cache_root: Path) -> None:
    weight_path = model_cache_root / "silero-vad" / "files" / "silero_vad.jit"
    weight_path.parent.mkdir(parents=True)
    weight_path.write_bytes(_SILERO_WEIGHT_BYTES)


def _fake_torch_with_hub_load(load_fn: object) -> ModuleType:
    fake_torch = ModuleType("torch")
    fake_hub = ModuleType("torch.hub")
    fake_hub.load = load_fn
    fake_torch.hub = fake_hub
    return fake_torch


def test_real_vad_engine_never_loads_from_a_remote_torch_hub_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed VAD benchmark must never call `torch.hub.load` against
    GitHub or any other network source -- only a locally staged snapshot,
    loaded with `source="local"`.
    """
    _stage_silero_snapshot(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_load(
        *, repo_or_dir: str, model: str, source: str, trust_repo: bool
    ) -> tuple[object, list[object]]:
        assert source == "local", "must never load from a remote Torch Hub source"
        assert repo_or_dir != "snakers4/silero-vad", "must never reference the remote GitHub repo"
        assert Path(repo_or_dir).is_dir(), "must load from an actual local directory"
        calls.append({"repo_or_dir": repo_or_dir, "model": model, "source": source})
        return object(), [lambda *a, **k: []]

    monkeypatch.setitem(sys.modules, "torch", _fake_torch_with_hub_load(fake_load))
    artifact = VerifiedModelArtifact(
        model_name="silero-vad-v6", model_version="v6", model_sha256=_SILERO_WEIGHT_SHA256
    )
    engine = audio_social_benchmark._build_real_vad_engine(
        model_cache_root=tmp_path, artifact=artifact
    )
    assert engine is not None
    assert len(calls) == 1
    assert calls[0]["source"] == "local"


def test_real_vad_engine_reaches_adapter_seam_with_valid_local_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validly staged, hash-matching local snapshot reaches the adapter
    seam and returns a usable `VadEngine` -- no network access involved.
    """
    _stage_silero_snapshot(tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "torch",
        _fake_torch_with_hub_load(lambda **kwargs: (object(), [lambda *a, **k: []])),  # noqa: ARG005
    )
    artifact = VerifiedModelArtifact(
        model_name="silero-vad-v6", model_version="v6", model_sha256=_SILERO_WEIGHT_SHA256
    )
    engine = audio_social_benchmark._build_real_vad_engine(
        model_cache_root=tmp_path, artifact=artifact
    )
    assert hasattr(engine, "detect_speech")
    assert callable(engine.detect_speech)


def test_real_vad_engine_blocked_when_local_snapshot_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch_with_hub_load(None))
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="snapshot"
    ):
        audio_social_benchmark._build_real_vad_engine(model_cache_root=tmp_path, artifact=artifact)


def test_real_vad_engine_blocked_when_snapshot_path_escapes_model_cache_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_cache_root = tmp_path / "cache"
    model_cache_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (model_cache_root / "silero-vad").symlink_to(outside, target_is_directory=True)

    monkeypatch.setitem(sys.modules, "torch", _fake_torch_with_hub_load(None))
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="a" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="escapes"
    ):
        audio_social_benchmark._build_real_vad_engine(
            model_cache_root=model_cache_root, artifact=artifact
        )


def test_real_vad_engine_blocked_on_artifact_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_silero_snapshot(tmp_path)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch_with_hub_load(None))
    artifact = VerifiedModelArtifact(model_name="x", model_version="1", model_sha256="b" * 64)
    with pytest.raises(
        audio_social_benchmark_adapters.BenchmarkArtifactUnavailableError, match="SHA-256"
    ):
        audio_social_benchmark._build_real_vad_engine(model_cache_root=tmp_path, artifact=artifact)


# --- No direct infrastructure write (static source scan) ------------------


@pytest.mark.parametrize(
    "module",
    [
        audio_social_benchmark,
        audio_social_benchmark_adapters,
        audio_social_benchmark_cli,
        audio_social_benchmark_metrics,
        audio_social_benchmark_validation,
    ],
)
def test_benchmark_module_never_imports_a_database_or_object_store_client(
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
        "import redis",
        "from redis",
        "import sqlalchemy",
        "from sqlalchemy",
    ):
        assert forbidden not in source, f"{module.__name__} unexpectedly references {forbidden!r}"


def test_run_benchmark_never_writes_a_local_root_string_into_the_result(tmp_path: Path) -> None:
    data_root = tmp_path / "some-private-data-root"
    output_root = tmp_path / "out"
    result = audio_social_benchmark.run_benchmark(
        dataset_id="common_voice_indic",
        candidate_id="faster-whisper-small",
        data_root=data_root,
        output_root=output_root,
    )
    assert str(data_root) not in result.model_dump_json()
