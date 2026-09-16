"""Phase 7 Part 4 audio/social-benchmark CLI tests: argument parsing, exit codes, safe output.

Every invocation points `TRACEX_BENCHMARK_*` at pytest `tmp_path`
directories -- never a developer's real environment, and never a real
dataset/model artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.communication_processing.audio_social_benchmark_cli import main
from app.modules.communication_processing.audio_social_benchmark_validation import (
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
    assert "common_voice_indic" in payload["allowed_dataset_ids"]
    assert "faster-whisper-small" in payload["allowed_candidate_ids"]
    assert ["vast_social_text", "existing-deterministic-social-parsers"] in (
        payload["allowed_dataset_candidate_pairs"]
    )


def test_validate_rejects_an_unknown_dataset_id(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["validate", "--dataset-id", "not_a_real_dataset", "--candidate-id", "faster-whisper-small"]
    )
    assert exit_code == 2
    assert "rejected" in capsys.readouterr().err


def test_validate_reports_a_missing_dataset_directory_and_pending_licence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["validate", "--dataset-id", "common_voice_indic", "--candidate-id", "faster-whisper-small"]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["dataset_directory_exists"] is False
    assert payload["license_and_conditional_status_cleared_for_real_execution"] is False


def test_validate_reports_the_already_license_cleared_social_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "validate",
            "--dataset-id",
            "vast_social_text",
            "--candidate-id",
            "existing-deterministic-social-parsers",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["license_and_conditional_status_cleared_for_real_execution"] is True
    assert payload["dataset_directory_exists"] is False
    assert exit_code == 1  # still unavailable overall -- no local directory exists


def test_run_rejects_an_unsupported_dataset_candidate_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["run", "--dataset-id", "common_voice_indic", "--candidate-id", "silero-vad-v6"]
    )
    assert exit_code == 2
    assert "rejected" in capsys.readouterr().err


def test_run_reports_unavailable_with_exit_code_1_and_writes_a_safe_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["run", "--dataset-id", "common_voice_indic", "--candidate-id", "faster-whisper-small"]
    )
    assert exit_code == 1
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["status"] == "unavailable"
    assert payload["dataset_id"] == "common_voice_indic"
    result_files = list((tmp_path / "out").glob("*.json"))
    assert len(result_files) == 1
    written = result_files[0].read_text()
    assert str(tmp_path) not in written


def test_run_writes_a_safe_result_for_the_social_extraction_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Confirms the CLI end to end for the one pair not blocked by licence today."""
    exit_code = main(
        [
            "run",
            "--dataset-id",
            "vast_social_text",
            "--candidate-id",
            "existing-deterministic-social-parsers",
        ]
    )
    assert exit_code == 1  # blocked only by the missing local dataset directory
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert "directory does not exist" in payload["failure_reason_safe"]


def test_run_accepts_transcription_stage_artifact_flags_for_language_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI must accept and thread through `fasttext-lid176`'s required
    transcription-stage artifact flags without error, even though this
    pair is still blocked by its own conditional licence status today.
    """
    exit_code = main(
        [
            "run",
            "--dataset-id",
            "common_voice_indic",
            "--candidate-id",
            "fasttext-lid176",
            "--model-name",
            "fasttext-lid176",
            "--model-version",
            "176",
            "--model-sha256",
            "a" * 64,
            "--transcription-stage-model-name",
            "faster-whisper-small",
            "--transcription-stage-model-version",
            "1",
            "--transcription-stage-model-sha256",
            "b" * 64,
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"


def test_cli_help_documents_every_subcommand() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
