"""Quality-report metadata only: no datasets, fitting or reserved holdouts."""
import json

import pytest

from scripts import export_review_package, quality_matrix


def evaluation(dataset, *, population=200):
    return {"dataset": dataset, "model": {"release_id": "frozen-fixture-release"},
        "results": {task: {"population": population, "prevalence": .25, "ap": .95,
            "p_at_100": .95 if population >= 100 else None, "review_budget": 10,
            "p_at_review_budget": .95, "recall_at_review_budget": .4,
            "benign_false_positives": 0} for task in quality_matrix.TASKS},
        "comparison": {task: {"ap_delta": .05} for task in quality_matrix.TASKS}}


def write_report(tmp_path, report):
    path = tmp_path / (report["dataset"] + ".json")
    path.write_text(json.dumps(report))
    return path


def test_missing_ap_and_p100_cannot_hide_behind_a_passing_dataset(tmp_path):
    good, missing = evaluation("measurable"), evaluation("unmeasurable")
    for task in quality_matrix.TASKS:
        missing["results"][task].update(ap=None, p_at_100=None)
    summary = quality_matrix.aggregate([write_report(tmp_path, good), write_report(tmp_path, missing)], 10)
    assert summary["status"] == "INCOMPLETE"
    assert summary["quality_gates_passed"] is False
    for task in quality_matrix.TASKS:
        assert summary["worst_case"][task]["ap"] is None
        assert summary["worst_case"][task]["p_at_100"] is None
        for gate in ("ap_preferred", "ap_stretch", "p100", "no_ap_regression"):
            assert summary["gates"][task][gate] is False
            details = summary["gate_details"][task][gate]
            assert details["status"] == "NOT EVALUABLE"
            assert details["reasons"][0]["dataset"] == "unmeasurable"
            if gate == "no_ap_regression":
                assert "Candidate AP is not measurable" in details["reasons"][0]["reason"]
            else:
                assert "missing/null" in details["reasons"][0]["reason"]


@pytest.mark.parametrize("field", ["ap", "p_at_100", "p_at_review_budget", "recall_at_review_budget", "benign_false_positives", "minimum_ap_delta"])
@pytest.mark.parametrize("absent", [False, True])
def test_required_null_or_absent_metrics_are_incomplete_not_partial_extrema(tmp_path, field, absent):
    good, missing = evaluation("complete"), evaluation("missing-" + field)
    row = missing["comparison"]["motif"] if field == "minimum_ap_delta" else missing["results"]["motif"]
    key = "ap_delta" if field == "minimum_ap_delta" else field
    if absent:
        del row[key]
    else:
        row[key] = None
    summary = quality_matrix.aggregate([write_report(tmp_path, good), write_report(tmp_path, missing)], 10)
    assert summary["status"] == "INCOMPLETE" and summary["quality_gates_passed"] is False
    output_field = "max_benign_false_positives" if field == "benign_false_positives" else field
    assert summary["worst_case"]["motif"][output_field] is None
    coverage = summary["metric_coverage"]["motif"][field]
    assert coverage["status"] == "INCOMPLETE"
    assert coverage["datasets"][1]["dataset"] == missing["dataset"]
    assert coverage["datasets"][1]["status"] == "NOT EVALUABLE"
    assert "missing/null" in coverage["datasets"][1]["reason"]


def test_p100_not_applicable_requires_known_small_population_and_is_not_a_pass(tmp_path):
    summary = quality_matrix.aggregate([write_report(tmp_path, evaluation("small", population=64))], 10)
    assert summary["status"] == "EVALUATED_NOT_PROMOTED"
    assert summary["quality_gates_passed"] is False
    details = summary["gate_details"]["motif"]["p100"]
    assert summary["gates"]["motif"]["p100"] is False
    assert details["status"] == "NOT APPLICABLE"
    assert "population is 64" in details["reasons"][0]["reason"]


def test_mixed_population_p100_checks_every_eligible_dataset_and_records_exclusions(tmp_path):
    paths = [write_report(tmp_path, evaluation("small", population=64)), write_report(tmp_path, evaluation("eligible"))]
    summary = quality_matrix.aggregate(paths, 10)
    assert summary["quality_gates_passed"] is True
    assert summary["worst_case"]["motif"]["p_at_100"] == .95
    reasons = summary["gate_details"]["motif"]["p100"]["reasons"]
    assert reasons[0]["dataset"] == "small" and reasons[0]["status"] == "NOT APPLICABLE"


def test_unknown_population_cannot_excuse_missing_p100(tmp_path):
    report = evaluation("unknown-population")
    report["results"]["motif"].update(population=None, p_at_100=None)
    summary = quality_matrix.aggregate([write_report(tmp_path, report)], 10)
    assert summary["status"] == "INCOMPLETE"
    assert summary["gate_details"]["motif"]["p100"]["status"] == "NOT EVALUABLE"
    assert "applicability is unknown" in summary["gate_details"]["motif"]["p100"]["reasons"][0]["reason"]


def test_no_positives_and_no_labelled_task_have_dataset_specific_reasons(tmp_path):
    report = evaluation("no-label-coverage")
    report["results"]["motif"].update(prevalence=0, ap=None, recall_at_review_budget=0)
    report["results"]["surge"] = {"status": "no labelled observations"}
    del report["results"]["discrimination"]
    summary = quality_matrix.aggregate([write_report(tmp_path, report)], 10)
    assert summary["status"] == "INCOMPLETE" and not summary["quality_gates_passed"]
    assert "No positive labels" in summary["gate_details"]["motif"]["ap_preferred"]["reasons"][0]["reason"]
    assert summary["worst_case"]["motif"]["recall_at_review_budget"] is None
    for task in ("surge", "discrimination"):
        assert "No labelled observations" in summary["gate_details"][task]["ap_preferred"]["reasons"][0]["reason"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "0.95", True, -0.1, 1.1])
def test_invalid_ap_cannot_pass_or_poison_the_minimum(tmp_path, value):
    report = evaluation("invalid-ap")
    report["results"]["motif"]["ap"] = value
    summary = quality_matrix.aggregate([write_report(tmp_path, evaluation("valid")), write_report(tmp_path, report)], 10)
    assert summary["status"] == "INCOMPLETE" and summary["quality_gates_passed"] is False
    assert summary["worst_case"]["motif"]["ap"] is None
    assert summary["gate_details"]["motif"]["ap_preferred"]["status"] == "NOT EVALUABLE"


def test_observed_zeroes_are_valid_measurements_not_missing_values(tmp_path):
    report = evaluation("zero-metrics")
    for task in quality_matrix.TASKS:
        report["results"][task].update(ap=0, p_at_100=0, p_at_review_budget=0, recall_at_review_budget=0)
        report["comparison"][task]["ap_delta"] = 0
    summary = quality_matrix.aggregate([write_report(tmp_path, report)], 10)
    assert summary["status"] == "EVALUATED_NOT_PROMOTED"
    assert all(value == 0 for value in summary["worst_case"]["motif"].values())
    assert summary["gates"]["motif"]["ap_preferred"] is False
    assert summary["gates"]["motif"]["no_ap_regression"] is True


def test_complete_worst_case_and_cli_threshold_failure(tmp_path):
    good, worse = evaluation("passing"), evaluation("regressed")
    worse["results"]["motif"].update(ap=.86, p_at_100=.89, benign_false_positives=3)
    worse["comparison"]["discrimination"]["ap_delta"] = -.02
    paths = [write_report(tmp_path, good), write_report(tmp_path, worse)]
    output = tmp_path / "worst.json"
    assert quality_matrix.main(["aggregate", "--result", str(paths[0]), "--result", str(paths[1]),
        "--review-budget", "10", "--output", str(output)]) == 1
    summary = json.loads(output.read_text())
    assert summary["status"] == "EVALUATED_NOT_PROMOTED" and summary["quality_gates_passed"] is False
    assert summary["worst_case"]["motif"]["ap"] == .86
    assert summary["worst_case"]["motif"]["max_benign_false_positives"] == 3
    assert summary["gates"]["motif"] == {"ap_preferred": True, "ap_stretch": False, "p100": False, "no_ap_regression": True}
    assert summary["gate_details"]["discrimination"]["no_ap_regression"]["status"] == "FAIL"


def test_cli_incomplete_saves_diagnostics_and_export_preserves_reasons(tmp_path):
    report = evaluation("missing-ap")
    report["results"]["motif"]["ap"] = None
    path = write_report(tmp_path, report)
    output = tmp_path / "incomplete.json"
    args = ["aggregate", "--result", str(path), "--review-budget", "10", "--output", str(output)]
    assert quality_matrix.main(args) == 2
    with pytest.raises(SystemExit):
        quality_matrix.main(args)
    package = tmp_path / "compact"
    export_review_package.main(["--result", str(output), "--output", str(package)])
    exported = json.loads((package / "result-00.json").read_text())
    assert exported["schema"] == "quality-matrix-v2" and exported["status"] == "INCOMPLETE"
    assert exported["quality_gates_passed"] is False
    assert exported["metric_coverage"]["motif"]["ap"]["status"] == "INCOMPLETE"
    assert exported["gate_details"]["motif"]["ap_preferred"]["reasons"][0]["dataset"] == "missing-ap"


def test_cli_zero_only_when_all_thresholds_are_evaluable_and_pass(tmp_path):
    path = write_report(tmp_path, evaluation("all-measurable"))
    output = tmp_path / "passed.json"
    assert quality_matrix.main(["aggregate", "--result", str(path), "--review-budget", "10", "--output", str(output)]) == 0
    summary = json.loads(output.read_text())
    assert summary["status"] == "EVALUATED_NOT_PROMOTED" and summary["quality_gates_passed"] is True
    assert all(type(value) is bool and value for task in summary["gates"].values() for value in task.values())


def test_unevaluable_deployed_queue_cannot_be_hidden_by_measurable_subset_ap(tmp_path):
    report = evaluation("partial-truth")
    report["queue_comparison"] = {"status": "NOT EVALUABLE", "eligible_transactions": 2000, "labelled_transactions": 200,
        "reasons": ["Unknown labels outside the subset; not measured deployed queue precision."]}
    summary = quality_matrix.aggregate([write_report(tmp_path, report)], 10)
    assert summary["status"] == "INCOMPLETE" and summary["quality_gates_passed"] is False
    assert summary["queue_coverage"][0]["dataset"] == "partial-truth"
    assert summary["queue_coverage"][0]["reasons"]


def test_budget_mismatch_stays_an_error_and_missing_budget_is_incomplete(tmp_path):
    report = evaluation("capacity")
    path = write_report(tmp_path, report)
    with pytest.raises(ValueError, match="review budgets differ"):
        quality_matrix.aggregate([path], 20)
    del report["results"]["motif"]["review_budget"]
    path.write_text(json.dumps(report))
    summary = quality_matrix.aggregate([path], 10)
    assert summary["status"] == "INCOMPLETE"
    assert "reviewer capacity is missing" in summary["metric_coverage"]["motif"]["p_at_review_budget"]["datasets"][0]["reason"]
    with pytest.raises(ValueError, match="at least one"):
        quality_matrix.aggregate([], 10)
