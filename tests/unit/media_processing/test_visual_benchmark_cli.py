"""Phase 7 Part 3 visual-benchmark CLI tests: argument parsing, exit codes, safe output.

Every invocation points `TRACEX_BENCHMARK_*` at pytest `tmp_path`
directories -- never a developer's real environment, and never a real
dataset/model artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.media_processing.visual_benchmark_cli import main
from app.modules.media_processing.visual_benchmark_validation import (
    ENV_BENCHMARK_DATA_ROOT,
    ENV_BENCHMARK_OUTPUT_ROOT,
    ENV_MODEL_CACHE_ROOT,
)


@pytest.fixture(autouse=True)
def _isolated_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_BENCHMARK_DATA_ROOT, str(tmp_path / "data"))
    monkeypatch.setenv(ENV_BENCHMARK_OUTPUT_ROOT, str(tmp_path / "out"))
    monkeypatch.delenv(ENV_MODEL_CACHE_ROOT, raising=False)


def test_list_candidates_prints_only_safe_aggregate_ids(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["list-candidates"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "virat_ground" in payload["allowed_dataset_ids"]
    assert "yolo11n" in payload["allowed_candidate_ids"]
    assert ["virat_ground", "bytetrack"] in payload["allowed_dataset_candidate_pairs"]


def test_validate_rejects_an_unknown_dataset_id(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["validate", "--dataset-id", "not_a_real_dataset", "--candidate-id", "yolo11n"]
    )
    assert exit_code == 2
    assert "rejected" in capsys.readouterr().err


def test_validate_reports_a_missing_dataset_directory_and_pending_licence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`safe_unsafe_behaviour` remains genuinely `pending_verification` (no
    pinned source in the frozen manifest) even after Gate B cleared
    `virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` -- see
    `docs/qa/known-limitations.md`.
    """
    exit_code = main(
        ["validate", "--dataset-id", "safe_unsafe_behaviour", "--candidate-id", "yolo11n"]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["dataset_directory_exists"] is False
    assert payload["license_cleared_for_real_execution"] is False


def test_validate_reports_the_already_license_cleared_virat_ground_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The genuinely discovered, real-verified pair Gate B cleared today --
    still `unavailable` overall in this isolated test only because no local
    dataset directory exists under the test's own `tmp_path` root.
    """
    exit_code = main(["validate", "--dataset-id", "virat_ground", "--candidate-id", "yolo11n"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["license_cleared_for_real_execution"] is True
    assert payload["dataset_directory_exists"] is False
    assert exit_code == 1  # still unavailable overall -- no local directory exists


def test_run_rejects_an_unsupported_dataset_candidate_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["run", "--dataset-id", "safe_unsafe_behaviour", "--candidate-id", "bytetrack"]
    )
    assert exit_code == 2
    assert "rejected" in capsys.readouterr().err


def test_run_reports_unavailable_with_exit_code_1_and_writes_a_safe_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["run", "--dataset-id", "virat_ground", "--candidate-id", "yolo11n"])
    assert exit_code == 1
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["status"] == "unavailable"
    assert payload["dataset_id"] == "virat_ground"
    result_files = list((tmp_path / "out").glob("*.json"))
    assert len(result_files) == 1
    written = result_files[0].read_text()
    assert str(tmp_path) not in written


def test_cli_help_documents_every_subcommand() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
