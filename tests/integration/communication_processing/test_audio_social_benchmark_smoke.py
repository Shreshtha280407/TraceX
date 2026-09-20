"""Phase 7 Part 4 local audio/social-benchmark smoke test.

Self-skips (never fabricates a pass) unless `TRACEX_BENCHMARK_DATA_ROOT`
is actually configured and the relevant dataset directory actually exists
locally -- exactly the state this development machine is expected to be
in (per this task's "do not download datasets/models on this machine"
rule) until Aditya's MacBook Gate B pre-flight populates real local
artifacts. Once those directories exist, this same test exercises the
real CLI end to end against them, as a local host process (never through
Docker Compose).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.communication_processing.audio_social_benchmark import dataset_subdirectory
from app.modules.communication_processing.audio_social_benchmark_validation import (
    ENV_BENCHMARK_DATA_ROOT,
    ENV_BENCHMARK_OUTPUT_ROOT,
    ENV_MODEL_CACHE_ROOT,
)
from app.modules.evaluation.manifest import load_dataset_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _configured_data_root() -> Path | None:
    raw = os.environ.get(ENV_BENCHMARK_DATA_ROOT)
    return Path(raw).expanduser() if raw and raw.strip() else None


def _skip_unless_dataset_present(dataset_id: str) -> Path:
    data_root = _configured_data_root()
    if data_root is None:
        pytest.skip(
            f"{ENV_BENCHMARK_DATA_ROOT} is not set -- see docs/runbooks/"
            f"local-development.md's Gate B MacBook pre-flight section"
        )
    manifest = load_dataset_manifest()
    dataset = manifest.get(dataset_id)
    assert dataset is not None  # frozen manifest invariant, not an environment condition
    dataset_dir = dataset_subdirectory(dataset, data_root)
    if not dataset_dir.is_dir() or not any(dataset_dir.iterdir()):
        pytest.skip(f"no local '{dataset_id}' dataset directory found under {data_root}")
    return dataset_dir


def _run_cli(*args: str, output_root: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env[ENV_BENCHMARK_OUTPUT_ROOT] = str(output_root)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, developer-controlled input
        [
            sys.executable,
            "-m",
            "app.modules.communication_processing.audio_social_benchmark_cli",
            *args,
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_asr_benchmark_cli_runs_against_a_real_local_common_voice_directory(tmp_path: Path) -> None:
    _skip_unless_dataset_present("common_voice_indic")
    if not os.environ.get(ENV_MODEL_CACHE_ROOT):
        pytest.skip(f"{ENV_MODEL_CACHE_ROOT} is not set -- no local model cache to benchmark")
    output_root = tmp_path / "out"
    completed = _run_cli(
        "run",
        "--dataset-id",
        "common_voice_indic",
        "--candidate-id",
        "faster-whisper-small",
        output_root=output_root,
    )
    assert completed.returncode in (0, 1), completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dataset_id"] == "common_voice_indic"
    assert payload["status"] in {"succeeded", "failed", "unavailable"}


def test_diarization_benchmark_cli_runs_against_a_real_local_ami_directory(tmp_path: Path) -> None:
    _skip_unless_dataset_present("ami_meeting_corpus")
    output_root = tmp_path / "out"
    completed = _run_cli(
        "run",
        "--dataset-id",
        "ami_meeting_corpus",
        "--candidate-id",
        "deterministic-diarization-fallback",
        output_root=output_root,
    )
    assert completed.returncode in (0, 1), completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] in {"succeeded", "failed", "unavailable"}


def test_social_extraction_benchmark_cli_runs_against_a_real_local_vast_directory(
    tmp_path: Path,
) -> None:
    """Needs no optional package and no model cache root -- see
    `docs/qa/known-limitations.md`'s note on this pair's already-cleared
    licence status."""
    _skip_unless_dataset_present("vast_social_text")
    output_root = tmp_path / "out"
    completed = _run_cli(
        "run",
        "--dataset-id",
        "vast_social_text",
        "--candidate-id",
        "existing-deterministic-social-parsers",
        output_root=output_root,
    )
    assert completed.returncode in (0, 1), completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] in {"succeeded", "failed", "unavailable"}
    if payload["status"] == "succeeded":
        assert payload["artifact_sha256"] is not None
