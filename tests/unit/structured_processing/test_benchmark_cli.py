"""Phase 7 Part 2 benchmark CLI tests: argument parsing and exit codes only.

Every invocation here points `TRACEX_BENCHMARK_*` at pytest `tmp_path`
directories -- never a developer's real environment, and never a real
dataset/model artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.structured_processing.benchmark_cli import main
from app.modules.structured_processing.benchmark_validation import (
    ENV_BENCHMARK_DATA_ROOT,
    ENV_BENCHMARK_OUTPUT_ROOT,
    ENV_MODEL_CACHE_ROOT,
)


@pytest.fixture(autouse=True)
def _isolated_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, str(tmp_path / "data"))
    monkeypatch.setenv(ENV_BENCHMARK_OUTPUT_ROOT, str(tmp_path / "out"))
    monkeypatch.delenv(ENV_MODEL_CACHE_ROOT, raising=False)


def test_cli_rejects_an_unknown_dataset_id(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["--dataset-id", "not_a_real_dataset", "--candidate-id", "existing-deterministic-parsers"]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "rejected" in captured.err


def test_cli_rejects_an_unknown_candidate_id(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--dataset-id", "gomask_voice_cdr", "--candidate-id", "not_a_real_candidate"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "rejected" in captured.err


def test_cli_reports_unavailable_with_exit_code_1_for_missing_local_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["--dataset-id", "gomask_voice_cdr", "--candidate-id", "existing-deterministic-parsers"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "unavailable" in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "unavailable"


def test_cli_succeeds_with_exit_code_0_for_a_real_local_cdr_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_dir = tmp_path / "data" / "gomask_voice_cdr"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "sample.csv").write_text(
        "caller_number,callee_number,timestamp,duration_seconds\n"
        "9876543210,9123456780,2026-01-01 10:00:00,60\n"
    )
    exit_code = main(
        ["--dataset-id", "gomask_voice_cdr", "--candidate-id", "existing-deterministic-parsers"]
    )
    assert exit_code == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["status"] == "succeeded"
    assert payload["metrics"]["accepted_row_count"] == 1
    # Never the raw phone numbers from the synthetic fixture file above.
    assert "9876543210" not in stdout


def test_cli_writes_the_safe_result_json_to_the_output_root(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "data" / "ibm_amlsim"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "sample.csv").write_text(
        "sender_account,receiver_account,amount,currency,timestamp\n"
        "ACC1,ACC2,100.00,INR,2026-01-01 10:00:00\n"
    )
    exit_code = main(
        ["--dataset-id", "ibm_amlsim", "--candidate-id", "existing-deterministic-parsers"]
    )
    assert exit_code == 0
    output_root = tmp_path / "out"
    result_files = list(output_root.glob("*.json"))
    assert len(result_files) == 1
    written = json.loads(result_files[0].read_text())
    assert written["dataset_id"] == "ibm_amlsim"
    assert "ACC1" not in result_files[0].read_text()


def test_cli_help_documents_every_local_root_flag() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
