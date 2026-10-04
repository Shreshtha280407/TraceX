"""Independently authored 64-row populations; no large/root generator workloads."""
import json

import pytest

from app.ml import candidate
from scripts import candidate_lifecycle as lifecycle
from scripts import quality_matrix


def test_four_backend_cli_training_freeze_transfer_stress_and_worst_case(tmp_path):
    protocol = tmp_path / "protocol.json"
    final, shifted = tmp_path / "fresh-final", tmp_path / "fresh-missing-network"
    lifecycle.main(["register", "--protocol", str(protocol), "--final", str(final), "--final", str(shifted)])
    datasets = []
    for i, role in enumerate(("training", "calibration", "validation")):
        directory = tmp_path / role
        quality_matrix.main(["fixture", "--output", str(directory), "--seed", str(i + 1), "--role", role])
        datasets.extend(["--" + role, str(directory), "--" + role + "-truth", str(directory / "ground_truth.json")])
    comparison = tmp_path / "comparison"
    assert lifecycle.main(["compare", "--protocol", str(protocol), *datasets, "--exclude-family", "independent_equal_outputs", "--finding-budget", ".5", "--review-budget", "10", "--output", str(comparison)]) == 0
    results = json.loads((comparison / "comparison.json").read_text())
    assert set(results["models"]) == {"hist", "xgboost", "lightgbm", "hybrid"}
    assert not any(results["split_overlap"].values())
    path = comparison / "hist"
    digest = candidate.sha(path / "manifest.json")
    # Freeze pins source/truth bytes without reading final labels. Registration
    # occurred before selection; generation is separate from final evaluation.
    quality_matrix.main(["fixture", "--output", str(final), "--seed", "4", "--role", "final"])
    quality_matrix.main(["stress", "--source", str(final), "--output", str(shifted), "--protocol", str(protocol), "--variant", "missing_network", "--seed", "5"])
    assert lifecycle.main(["freeze", "--protocol", str(protocol), "--artifact", str(path), "--manifest-sha256", digest,
        "--validation-report", str(comparison / "comparison.json"), "--reason", "Tiny wiring test, NOT promotion or AP acceptance"]) == 0
    reports = []
    for dataset in (final, shifted):
        report = tmp_path / (dataset.name + ".json")
        with pytest.raises(SystemExit):
            lifecycle.main(["evaluate", "--protocol", str(protocol), "--artifact", str(path), "--manifest-sha256", digest,
                "--dataset", str(dataset), "--truth", str(dataset / "ground_truth.json"), "--output", str(report), "--review-budget", "11", "--finding-budget", ".5"])
        assert lifecycle.main(["evaluate", "--protocol", str(protocol), "--artifact", str(path), "--manifest-sha256", digest,
            "--dataset", str(dataset), "--truth", str(dataset / "ground_truth.json"), "--output", str(report), "--review-budget", "10", "--finding-budget", ".5"]) == 0
        reports.append(report)
        payload = json.loads(report.read_text())
        assert payload["results"]["motif"]["p_at_100"] is None
        assert "retrospective" in payload["baseline"]["procedure"]
        assert payload["queue_comparison"]["candidate_available_findings"] == 32
    summary = quality_matrix.aggregate(reports, 10)
    assert set(summary["worst_case"]) == set(candidate.TASKS)
    assert summary["status"] == "EVALUATED_NOT_PROMOTED"
    assert summary["quality_gates_passed"] is False  # 64 rows cannot exercise P@100.
    for task in candidate.TASKS:
        assert summary["gates"][task]["p100"] is False
        assert summary["gate_details"][task]["p100"]["status"] == "NOT APPLICABLE"
    with pytest.raises(FileExistsError):
        lifecycle.main(["evaluate", "--protocol", str(protocol), "--artifact", str(path), "--manifest-sha256", digest,
            "--dataset", str(final), "--truth", str(final / "ground_truth.json"), "--output", str(tmp_path / "forbidden.json"),
            "--review-budget", "10", "--finding-budget", ".5"])
    with pytest.raises(SystemExit):
        lifecycle.main(["train", "--protocol", str(protocol), *datasets, "--output", str(tmp_path / "forbidden-selection")])


@pytest.mark.parametrize("variant", quality_matrix.VARIANTS)
def test_stress_is_label_free_bounded_and_preserves_truth(tmp_path, variant):
    source, dest = tmp_path / "source", tmp_path / "final"
    quality_matrix.independent_fixture(source, 64, 10, "stress-check")
    quality_matrix.stress(source, dest, variant, 11)
    assert candidate.sha(source / "ground_truth.json") == candidate.sha(dest / "ground_truth.json")
    matrix, labels, _, _ = lifecycle.population(dest, dest / "ground_truth.json")
    assert len(matrix) == 64 and len(labels["motif"]) == 64
    assert json.loads((dest / "quality_manifest.json").read_text())["provenance"]["variant"] == variant


def test_fixture_cannot_accidentally_generate_large_data(tmp_path):
    with pytest.raises(ValueError, match="1000"):
        quality_matrix.independent_fixture(tmp_path / "never-created", 100000, 1, "unsafe")
    assert not (tmp_path / "never-created").exists()
