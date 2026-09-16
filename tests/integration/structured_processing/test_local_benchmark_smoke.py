"""Phase 7 Part 2 local benchmark smoke test.

Self-skips (never fabricates a pass) unless `TRACEX_BENCHMARK_DATA_ROOT`
is actually configured and the relevant dataset directory actually exists
locally -- exactly the state this development machine is expected to be
in (per this task's "do not download public datasets... on Shreshtha's
laptop" rule) until Aditya's MacBook pre-flight populates real local
artifacts. Once those directories exist (on the MacBook, or any other
machine that has legitimately downloaded the approved datasets), this
same test exercises the real CLI end to end against them.

This is deliberately not a fixture-and-fake-engine test (that is what
`tests/unit/structured_processing/test_benchmark_*.py` already cover
exhaustively) -- it is the one place this task's own real
`benchmark_cli.main` is invoked as a subprocess against whatever the
environment actually provides, proving the CLI wiring itself (not just
the underlying functions) works end to end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.structured_processing.benchmark import dataset_subdirectory
from app.modules.structured_processing.benchmark_validation import (
    ENV_BENCHMARK_DATA_ROOT,
    ENV_BENCHMARK_OUTPUT_ROOT,
    ENV_MODEL_CACHE_ROOT,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _configured_data_root() -> Path | None:
    raw = os.environ.get(ENV_BENCHMARK_DATA_ROOT)
    return Path(raw).expanduser() if raw and raw.strip() else None


def _skip_unless_dataset_present(dataset_id: str) -> Path:
    data_root = _configured_data_root()
    if data_root is None:
        pytest.skip(
            f"{ENV_BENCHMARK_DATA_ROOT} is not set -- see docs/runbooks/"
            f"local-development.md's MacBook pre-flight section"
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
        [sys.executable, "-m", "app.modules.structured_processing.benchmark_cli", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_cdr_benchmark_cli_runs_against_a_real_local_gomask_directory(tmp_path: Path) -> None:
    _skip_unless_dataset_present("gomask_voice_cdr")
    output_root = tmp_path / "out"
    completed = _run_cli(
        "--dataset-id",
        "gomask_voice_cdr",
        "--candidate-id",
        "existing-deterministic-parsers",
        output_root=output_root,
    )
    assert completed.returncode in (0, 1), completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dataset_id"] == "gomask_voice_cdr"
    assert payload["status"] in {"succeeded", "failed", "unavailable"}
    result_files = list(output_root.glob("*.json"))
    assert len(result_files) == 1


def test_finance_benchmark_cli_runs_against_a_real_local_amlsim_directory(tmp_path: Path) -> None:
    _skip_unless_dataset_present("ibm_amlsim")
    output_root = tmp_path / "out"
    completed = _run_cli(
        "--dataset-id",
        "ibm_amlsim",
        "--candidate-id",
        "existing-deterministic-parsers",
        output_root=output_root,
    )
    assert completed.returncode in (0, 1), completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dataset_id"] == "ibm_amlsim"
    assert payload["status"] in {"succeeded", "failed", "unavailable"}


def test_ocr_benchmark_cli_runs_against_a_real_local_fir_icdar_directory(
    tmp_path: Path,
) -> None:
    _skip_unless_dataset_present("fir_icdar_2023")
    model_cache_root = os.environ.get(ENV_MODEL_CACHE_ROOT)
    if not model_cache_root:
        pytest.skip(f"{ENV_MODEL_CACHE_ROOT} is not set -- no local model cache to benchmark")
    output_root = tmp_path / "out"
    completed = _run_cli(
        "--dataset-id",
        "fir_icdar_2023",
        "--candidate-id",
        "paddleocr-ppocrv5-mobile",
        output_root=output_root,
    )
    assert completed.returncode in (0, 1), completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dataset_id"] == "fir_icdar_2023"
    assert payload["status"] in {"succeeded", "failed", "unavailable"}
    if payload["status"] == "succeeded":
        assert payload["artifact_sha256"] is not None
